/** `GET /desk/regime/backtest` (Accept: application/json): `{b, path, charts}`; geometry is precomputed by the desk. */
export interface Tick {
  px: number;
  v?: number;
  label?: string;
}

export interface ScatterMark {
  key: string;
  x: number;
  y: number;
  lx: number;
  ly: number;
  anchor: "start" | "end" | "middle";
  color: string;
  leader: boolean;
  best: boolean;
  proxy: boolean;
  cagr: number | string;
  dd: number | string;
}

export interface Scatter {
  width: number;
  height: number;
  pad_l: number;
  pad_r: number;
  pad_t: number;
  pad_b: number;
  plot_w: number;
  plot_h: number;
  xticks: Tick[];
  yticks: Tick[];
  families: { label: string; color: string }[];
  marks: ScatterMark[];
}

export interface CurveLine {
  key: string;
  color: string;
  callout: boolean;
  equity: string;
  drawdown: string;
  exposure: string | null;
  label_y: number;
  final: number;
  avg_exposure: number | null;
}

export interface Curves {
  start: string;
  end: string;
  width: number;
  pad_l: number;
  pad_r: number;
  pad_t: number;
  eq_h: number;
  dd_h: number;
  ex_h: number;
  eq_ticks: Tick[];
  dd_ticks: Tick[];
  ex_ticks: Tick[];
  xticks: Tick[];
  lines: CurveLine[];
  has_exposure: boolean;
  exposure_window: number;
}

export interface Subperiods {
  bar_w: number;
  rows: {
    label: string;
    bars: { key: string; cagr: number; dd: number; cagr_w: number; dd_w: number; color: string }[];
  }[];
}

export interface Timeline {
  label: string;
  start: string;
  end: string;
  unavailable?: string | null;
  width: number;
  height: number;
  pad_l: number;
  pad_t: number;
  ticks: Tick[];
  area: string;
  path: string;
  changes: { x: number }[];
  first: string;
  last: string;
  min_exposure: number;
}

export interface Robustness {
  min: number | string;
  max: number | string;
  ramp: string[];
  ink: string;
  confirms: number[];
  buffers: number[];
  grids: {
    r4: number;
    cells: { buffer: number; confirm: number; value: number; dd: number | string; whip: number | string; color: string }[];
  }[];
  narrow: boolean;
  spread: number | string;
  r4_inert: boolean;
}

export interface Bars {
  width: number;
  height: number;
  pad_l: number;
  bars: { key: string; label?: string; x: number; y: number; w: number; color: string; value: number }[];
}

export interface Charts {
  scatter?: Scatter | null;
  breadth_caveat?: string | null;
  curves?: Curves | null;
  subperiods?: Subperiods | null;
  timelines?: Timeline[] | null;
  robustness?: Robustness | null;
  sharpe?: Bars | null;
  ablation?: Bars | null;
}

export interface Variant {
  label: string;
  status: string;
  note?: string | null;
  reason?: string | null;
  CAGR: number;
  max_drawdown_pct: number;
  volatility_pct: number;
  sharpe: number;
  sortino: number;
  calmar: number;
  avg_equity_exposure: number;
  pct_time_in_cash: number;
  transitions: number;
  transitions_per_year: number;
  whipsaws_per_year: number;
  annual_turnover_pct: number;
  total_cost_drag_pct_yr: number;
  roll_1Y_worst_pct: number | null;
  roll_3Y_med_pct: number | null;
  roll_5Y_med_pct: number | null;
}

export interface Backtest {
  generated_for: { start: string; end: string };
  limitations: Record<string, string>;
  variants: Record<string, Variant>;
  tier_distribution: Record<string, Record<string, number>>;
  cost_breakdown: Record<string, number>;
  cost_one_way: number;
  cost_round_trip: number;
  whipsaw_sensitivity: Record<string, number>;
  unavailable?: Record<string, string> | null;
}

export interface BacktestPage {
  b: Backtest | null;
  path: string;
  charts: Charts | null;
}
