"use client";

import { Send, Square } from "lucide-react";
import { useCallback, useEffect, useId, useState } from "react";

import { OptionsResultView } from "@/components/opportunities/options-results";
import { ErrorPanel } from "@/components/opportunities/states";
import { SwingResultView } from "@/components/opportunities/swing-results";
import { Button } from "@/components/ui/button";
import {
  EMPTY_ASK_STATE,
  buildRunInput,
  reduceAgui,
  threadMessages,
  type AskState,
  type ThreadItem,
} from "@/lib/opportunities/agui";
import { isAbort, runAgui } from "@/lib/opportunities/client";
import { toProblem } from "@/lib/opportunities/hooks";
import { createLatestGuard } from "@/lib/opportunities/latest";
import type { ProblemBody } from "@/lib/opportunities/types";

export function askExamples(symbol?: string): string[] {
  return [
    "Find liquid swing setups for the next 5–15 trading sessions.",
    "Breakouts in Nifty 200 with reward to risk of at least 2.",
    `Compare bull call spreads for ${symbol ?? "INFY"} within a maximum loss of ₹5,000.`,
  ];
}

const STEP_LABELS: Record<string, string> = {
  interpret: "Interpreting your request",
  scan_swing: "Running the swing scan",
  compare_options: "Comparing option structures",
};

function newId(prefix: string): string {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${random}`;
}

function horizonFromArgs(args: string): number {
  try {
    const parsed = JSON.parse(args) as unknown;
    if (typeof parsed === "object" && parsed !== null) {
      const value = (parsed as Record<string, unknown>).horizon_max;
      if (typeof value === "number") return value;
    }
  } catch (_error) {
    return 15;
  }
  return 15;
}

function ToolBlock({ item }: { item: Extract<ThreadItem, { kind: "tool" }> }) {
  const name = item.name === "scan_swing" ? "Swing scan" : item.name === "compare_options" ? "Options comparison" : item.name;
  return (
    <div className="space-y-2 rounded-md border border-border/60 p-3" data-testid={`tool-${item.name}`}>
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {name}
        {item.result ? "" : item.done ? " — waiting for result…" : " — running…"}
      </p>
      {item.result?.tool === "scan_swing" ? (
        <SwingResultView response={item.result.data} horizonMax={horizonFromArgs(item.args)} />
      ) : item.result?.tool === "compare_options" ? (
        <OptionsResultView analysis={item.result.data} />
      ) : item.result ? (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap text-xs text-muted-foreground">{item.result.raw}</pre>
      ) : null}
    </div>
  );
}

export function AskPanel({ symbol }: { symbol?: string | undefined } = {}) {
  const inputId = useId();
  const [threadId] = useState(() => newId("thread"));
  const [guard] = useState(createLatestGuard);
  const [state, setState] = useState<AskState>(EMPTY_ASK_STATE);
  const [draft, setDraft] = useState("");
  const [problem, setProblem] = useState<ProblemBody | null>(null);

  useEffect(() => () => guard.cancel(), [guard]);

  const send = useCallback(
    async (text: string) => {
      const content = text.trim();
      if (!content) return;
      const userItem: ThreadItem = { kind: "message", id: newId("msg"), role: "user", content, streaming: false };
      const base: AskState = { ...state, items: [...state.items, userItem], running: true, error: null };
      setState(base);
      setDraft("");
      setProblem(null);
      const ticket = guard.begin();
      try {
        await runAgui(buildRunInput(threadId, newId("run"), threadMessages(base), symbol), {
          signal: ticket.signal,
          onEvent: (event) => {
            if (guard.isCurrent(ticket.id)) setState((prev) => reduceAgui(prev, event));
          },
        });
        if (guard.isCurrent(ticket.id)) setState((prev) => ({ ...prev, running: false, step: null }));
      } catch (error) {
        if (!guard.isCurrent(ticket.id)) return;
        setState((prev) => ({ ...prev, running: false, step: null }));
        if (!isAbort(error)) setProblem(toProblem(error));
      }
    },
    [guard, state, threadId, symbol],
  );

  const stop = () => {
    guard.cancel();
    setState((prev) => ({ ...prev, running: false, step: null }));
  };

  return (
    <section aria-labelledby={`${inputId}-h`} className="space-y-3 rounded-lg border border-border/70 bg-card p-4" data-testid="ask-panel">
      <h2 id={`${inputId}-h`} className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
        Ask
      </h2>
      <div className="flex flex-wrap gap-2">
        {askExamples(symbol).map((example) => (
          <button
            key={example}
            type="button"
            onClick={() => setDraft(example)}
            className="rounded-full border border-border/70 px-3 py-1 text-xs hover:border-accent hover:text-accent"
          >
            {example}
          </button>
        ))}
      </div>
      {state.items.length > 0 ? (
        <ol className="space-y-3" aria-live="polite">
          {state.items.map((item) => (
            <li key={item.id}>
              {item.kind === "message" ? (
                <div
                  className={
                    item.role === "user"
                      ? "ml-auto max-w-[80%] rounded-md bg-muted px-3 py-2 text-sm"
                      : "max-w-[90%] whitespace-pre-wrap text-sm"
                  }
                >
                  {item.content}
                  {item.streaming ? <span className="ml-0.5 animate-pulse">▍</span> : null}
                </div>
              ) : (
                <ToolBlock item={item} />
              )}
            </li>
          ))}
        </ol>
      ) : null}
      {state.running ? (
        <p className="text-xs text-muted-foreground">{state.step ? `${STEP_LABELS[state.step] ?? state.step}…` : "Thinking…"}</p>
      ) : null}
      {state.error ? <ErrorPanel problem={{ title: "The run failed", detail: state.error, status: 0 }} /> : null}
      {problem ? <ErrorPanel problem={problem} /> : null}
      <form
        className="flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          void send(draft);
        }}
      >
        <label htmlFor={inputId} className="sr-only">
          Ask about opportunities
        </label>
        <textarea
          id={inputId}
          rows={2}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send(draft);
            }
          }}
          placeholder="Ask for a scan or an options comparison…"
          className="min-h-[2.25rem] flex-1 resize-y rounded-md border border-input bg-card px-3 py-2 text-sm"
        />
        {state.running ? (
          <Button variant="outline" onClick={stop} aria-label="Stop">
            <Square aria-hidden />
          </Button>
        ) : (
          <Button type="submit" variant="primary" disabled={!draft.trim()} aria-label="Send">
            <Send aria-hidden />
          </Button>
        )}
      </form>
      <p className="text-xs text-muted-foreground">
        Answers are research output, not advice. Nothing here places an order.
      </p>
    </section>
  );
}
