"use client";

import type { BacktestConfigIn, ScreenOut } from "@decile/api-client";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

/**
 * docs/08 §Backtests: "Config form (screen, date range, rebalance frequency, top N, weighting,
 * costs)".
 *
 * Every control below maps to one key of docs/10 §Config, and the defaults are docs/10's own
 * defaults, not invented ones — a form pre-filled with something the specification does not say
 * would quietly become the product's answer to "what does a backtest mean here".
 *
 * The two fields docs/08 does not list but docs/10 §Config does — the hold buffer and the
 * position limits — are here because leaving them out would make the form unable to express the
 * rank-buffer rule the whole product is built on (docs/01 §8).
 */
export interface ConfigFormProps {
  screens: readonly ScreenOut[];
  submitting: boolean;
  onSubmit: (config: BacktestConfigIn) => void;
  /** docs/01 §2.13: historical data starts here, so no earlier date can be simulated. */
  earliest: string;
  latest: string;
}

type Frequency = BacktestConfigIn["rebalance"]["frequency"];
type Weighting = BacktestConfigIn["weighting"];
type CashPolicy = BacktestConfigIn["cash_policy"];

const FREQUENCIES: readonly { value: Frequency; label: string }[] = [
  { value: "monthly", label: "Monthly" },
  { value: "quarterly", label: "Quarterly" },
  { value: "fortnightly", label: "Fortnightly" },
  { value: "weekly", label: "Weekly" },
];

const WEIGHTINGS: readonly { value: Weighting; label: string }[] = [
  { value: "equal", label: "Equal weight" },
  { value: "marketcap", label: "Marketcap weight" },
  { value: "rank", label: "Rank weight" },
  { value: "inverse_volatility", label: "Inverse volatility" },
];

const CASH_POLICIES: readonly { value: CashPolicy; label: string }[] = [
  { value: "hold_cash", label: "Hold cash" },
  { value: "benchmark", label: "Track the benchmark" },
];

/** The universes docs/01 §2.1 offers, used here only as the benchmark to compare against. */
const BENCHMARKS = [
  { value: "nifty-500", label: "NIFTY 500" },
  { value: "nifty-50", label: "NIFTY 50" },
  { value: "nifty-100", label: "NIFTY 100" },
  { value: "nifty-200", label: "NIFTY 200" },
  { value: "nifty-midcap-150", label: "NIFTY MIDCAP 150" },
  { value: "nifty-smallcap-250", label: "NIFTY SMALLCAP 250" },
] as const;

export function ConfigForm({
  screens,
  submitting,
  onSubmit,
  earliest,
  latest,
}: ConfigFormProps) {
  const [screenId, setScreenId] = useState(screens[0]?.public_id ?? "");
  const [start, setStart] = useState(earliest);
  const [end, setEnd] = useState(latest);
  const [capital, setCapital] = useState("1000000");
  const [frequency, setFrequency] = useState<Frequency>("monthly");
  const [topN, setTopN] = useState("20");
  const [holdBuffer, setHoldBuffer] = useState("10");
  const [weighting, setWeighting] = useState<Weighting>("equal");
  const [maxWeight, setMaxWeight] = useState("10");
  const [minWeight, setMinWeight] = useState("1");
  const [brokerage, setBrokerage] = useState("3");
  const [stt, setStt] = useState("10");
  const [slippage, setSlippage] = useState("15");
  const [benchmark, setBenchmark] = useState<string>("nifty-500");
  const [cashPolicy, setCashPolicy] = useState<CashPolicy>("hold_cash");
  const [overlay, setOverlay] = useState(false);

  const invalid = !screenId || start >= end;

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (invalid) return;
    onSubmit({
      screen_public_id: screenId,
      start,
      end,
      initial_capital: capital,
      rebalance: { frequency, day: "last_trading_day" },
      selection: { top_n: Number(topN), hold_buffer: Number(holdBuffer) },
      weighting,
      // The form asks for percentages because that is how a user thinks about a position cap;
      // docs/10 §Config stores fractions. The conversion happens once, here.
      position_limits: {
        max_weight: String(Number(maxWeight) / 100),
        min_weight: String(Number(minWeight) / 100),
      },
      costs: {
        brokerage_bps: brokerage,
        stt_bps: stt,
        slippage_bps: slippage,
        impact_model: "fixed",
      },
      cash_policy: cashPolicy,
      benchmark,
      risk_overlay: { enabled: overlay, rule: "index_above_200dma" },
      // Both are docs/10 §Config keys the form does not expose. `reinvest` is the only dividend
      // policy this service can serve (see `DividendPolicy` in `decile_core.backtest`), and the
      // risk-free rate has no T-bill series behind it, so offering either as a control would be
      // offering a choice that is not there. Sent explicitly rather than left to the server's
      // default, so the stored config says what the run actually used.
      dividends: "reinvest",
      risk_free_rate: "0",
    });
  }

  return (
    <form onSubmit={submit} className="space-y-6 rounded-lg border border-border bg-card p-4">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Field label="Screen" htmlFor="bt-screen">
          <Select
            id="bt-screen"
            value={screenId}
            onChange={(event) => setScreenId(event.target.value)}
          >
            {screens.length === 0 ? <option value="">No screens saved yet</option> : null}
            {screens.map((screen) => (
              <option key={screen.public_id} value={screen.public_id}>
                {screen.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Start" htmlFor="bt-start" hint={`Data starts ${earliest}.`}>
          <Input
            id="bt-start"
            type="date"
            value={start}
            min={earliest}
            max={latest}
            onChange={(event) => setStart(event.target.value)}
          />
        </Field>

        <Field label="End" htmlFor="bt-end">
          <Input
            id="bt-end"
            type="date"
            value={end}
            min={earliest}
            max={latest}
            onChange={(event) => setEnd(event.target.value)}
          />
        </Field>

        <Field label="Initial capital (₹)" htmlFor="bt-capital">
          <Input
            id="bt-capital"
            type="number"
            min="1000"
            step="1000"
            value={capital}
            onChange={(event) => setCapital(event.target.value)}
          />
        </Field>

        <Field label="Rebalance" htmlFor="bt-frequency" hint="On the last trading day of each period.">
          <Select
            id="bt-frequency"
            value={frequency}
            onChange={(event) => setFrequency(event.target.value as Frequency)}
          >
            {FREQUENCIES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Weighting" htmlFor="bt-weighting">
          <Select
            id="bt-weighting"
            value={weighting}
            onChange={(event) => setWeighting(event.target.value as Weighting)}
          >
            {WEIGHTINGS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Top N" htmlFor="bt-topn" hint="How many names the screen's top slice holds.">
          <Input
            id="bt-topn"
            type="number"
            min="1"
            max="500"
            value={topN}
            onChange={(event) => setTopN(event.target.value)}
          />
        </Field>

        <Field
          label="Hold buffer"
          htmlFor="bt-buffer"
          hint="Keep a holding while its rank stays inside Top N + buffer. 0 is strict top-N."
        >
          <Input
            id="bt-buffer"
            type="number"
            min="0"
            max="500"
            value={holdBuffer}
            onChange={(event) => setHoldBuffer(event.target.value)}
          />
        </Field>

        <Field label="Benchmark" htmlFor="bt-benchmark">
          <Select
            id="bt-benchmark"
            value={benchmark}
            onChange={(event) => setBenchmark(event.target.value)}
          >
            {BENCHMARKS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Max weight (%)" htmlFor="bt-max">
          <Input
            id="bt-max"
            type="number"
            min="0.1"
            max="100"
            step="0.5"
            value={maxWeight}
            onChange={(event) => setMaxWeight(event.target.value)}
          />
        </Field>

        <Field label="Min weight (%)" htmlFor="bt-min">
          <Input
            id="bt-min"
            type="number"
            min="0"
            max="100"
            step="0.5"
            value={minWeight}
            onChange={(event) => setMinWeight(event.target.value)}
          />
        </Field>

        <Field label="Uninvested cash" htmlFor="bt-cash">
          <Select
            id="bt-cash"
            value={cashPolicy}
            onChange={(event) => setCashPolicy(event.target.value as CashPolicy)}
          >
            {CASH_POLICIES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      <fieldset className="grid gap-4 sm:grid-cols-3">
        <legend className="mb-2 text-sm font-medium">Costs, in basis points of traded notional</legend>
        <Field label="Brokerage" htmlFor="bt-brokerage">
          <Input
            id="bt-brokerage"
            type="number"
            min="0"
            step="0.5"
            value={brokerage}
            onChange={(event) => setBrokerage(event.target.value)}
          />
        </Field>
        <Field label="STT" htmlFor="bt-stt">
          <Input
            id="bt-stt"
            type="number"
            min="0"
            step="0.5"
            value={stt}
            onChange={(event) => setStt(event.target.value)}
          />
        </Field>
        <Field label="Slippage" htmlFor="bt-slippage">
          <Input
            id="bt-slippage"
            type="number"
            min="0"
            step="0.5"
            value={slippage}
            onChange={(event) => setSlippage(event.target.value)}
          />
        </Field>
      </fieldset>

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={overlay}
          onChange={(event) => setOverlay(event.target.checked)}
          className="size-4 rounded border-border"
        />
        <span>
          Go to cash while the benchmark is below its 200-day moving average
          <span className="block text-xs text-muted-foreground">
            docs/10&rsquo;s only risk overlay. Off by default.
          </span>
        </span>
      </label>

      <div className="flex items-center gap-3">
        <Button type="submit" disabled={invalid || submitting}>
          {submitting ? "Queueing…" : "Run backtest"}
        </Button>
        {start >= end ? (
          <p className="text-sm text-destructive">The end date must be after the start date.</p>
        ) : null}
        {screens.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Save a screen first — a backtest runs a screen on past dates.
          </p>
        ) : null}
      </div>
    </form>
  );
}

interface FieldProps {
  label: string;
  htmlFor: string;
  hint?: string;
  children: React.ReactNode;
}

/** docs/08 §Accessibility: "Every form field has a real `<label>`; sentinel explanations use
 * `aria-describedby`." */
function Field({ label, htmlFor, hint, children }: FieldProps) {
  const hintId = `${htmlFor}-hint`;
  return (
    <div className="space-y-1.5">
      <Label htmlFor={htmlFor}>{label}</Label>
      <div aria-describedby={hint ? hintId : undefined}>{children}</div>
      {hint ? (
        <p id={hintId} className="text-xs text-muted-foreground">
          {hint}
        </p>
      ) : null}
    </div>
  );
}
