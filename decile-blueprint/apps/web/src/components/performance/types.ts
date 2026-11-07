// Shape of the desk's `GET /desk/performance/data` (performance_view.build).

export interface SampleFlag {
  n: number;
  need: number;
  meaningful: boolean;
}

export interface PerfMetric {
  label: string;
  value: number | null;
  unit: string;
  polarity: string; // "up" | "down" | "loss"
  flag: SampleFlag;
  note: string;
}

export interface PerfSeries {
  name: string;
  /** Pre-scaled SVG polyline points in a 1000 x 260 box. */
  points: string;
  last: number;
  type: string;
}

export interface PerfRelative {
  name: string;
  series_type: string;
  beta: number | null;
  alpha: number | null;
  tracking_error: number | null;
  information_ratio: number | null;
  up_capture: number | null;
  down_capture: number | null;
}

export interface PerfTrades {
  closed: number;
  win_rate: number;
  avg_win: number;
  avg_loss: number;
  payoff: number;
  profit_factor: number;
  expectancy: number;
  avg_hold_days: number;
  by_symbol: { symbol: string; pnl: number; trades: number; share_pct: number }[];
}

export interface PerformanceData {
  available: boolean;
  message?: string;
  how_to?: string;
  inception: string;
  as_of: string;
  observations: number;
  index_value: number;
  nav: number;
  cash: number;
  cash_pct: number;
  positions: number;
  metrics: PerfMetric[];
  drawdown: {
    depth_pct: number;
    peak: string | null;
    trough: string | null;
    recovered: boolean;
    recovery: string | null;
    duration_days: number;
    current_pct: number;
  };
  chart: { series: PerfSeries[]; lo: number; hi: number; start: string; end: string };
  underwater: { points: string; worst: number };
  series_used: Record<string, string>;
  dropped_benchmarks: string[];
  benchmark_notes: string[];
  relative: PerfRelative | null;
  relative_flag: SampleFlag;
  monthly: { years: number[]; rows: (number | null)[][] } | null;
  monthly_flag: SampleFlag;
  trades: PerfTrades | null;
  has_cashflows: boolean;
}
