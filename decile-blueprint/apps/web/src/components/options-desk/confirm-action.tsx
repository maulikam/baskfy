"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { useIsStaff } from "@/components/shell/staff-context";

import { answerText, sendToDesk, type ConsolePost, type DeskAnswer } from "./api";

function useSecondsLeft(expiresMs: number | null): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (expiresMs === null) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [expiresMs]);
  return expiresMs === null ? null : Math.floor((expiresMs - now) / 1000);
}

/**
 * Two clicks and one POST: the label arms it, the review step sends it with `confirm=true` and the
 * desk's own `plan_id` (or `session_id` for a close). Never retried; staff only.
 */
export function ConfirmAction({
  post,
  label,
  sentence,
  expiresMs,
  danger,
  onDone,
}: {
  post: ConsolePost;
  label: string;
  sentence: string | null;
  expiresMs: number | null;
  danger?: boolean;
  onDone: () => void;
}) {
  const staff = useIsStaff();
  const [stage, setStage] = useState<"idle" | "review" | "sending" | "sent">("idle");
  const [answer, setAnswer] = useState<DeskAnswer | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const left = useSecondsLeft(expiresMs);
  const expired = left !== null && left <= 0;

  if (!staff) return null;

  async function send() {
    if (stage !== "review" || expired) return;
    setStage("sending");
    try {
      const a = await sendToDesk(post);
      setAnswer(a);
      if (a.ok) setTimeout(onDone, 1500);
    } catch (error) {
      setFailure(`the request failed: ${error instanceof Error ? error.message : String(error)}`);
    }
    setStage("sent");
  }

  const countdown =
    left === null
      ? null
      : expired
        ? "PLAN_EXPIRED — the 30-minute window has passed"
        : `lapses in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border/70 bg-muted/30 p-3">
      {stage === "idle" && (
        <div className="flex flex-wrap items-center gap-3">
          <Button
            size="sm"
            variant={danger ? "destructive" : "primary"}
            disabled={expired}
            onClick={() => setStage("review")}
          >
            {label}
          </Button>
          {countdown && <span className="text-xs text-muted-foreground tabular-nums">{countdown}</span>}
        </div>
      )}
      {stage === "review" && (
        <div className="flex flex-col gap-2">
          {sentence && <p className="text-sm leading-relaxed">{sentence}</p>}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant={danger ? "destructive" : "primary"}
              disabled={expired}
              onClick={() => void send()}
            >
              Send to the desk
            </Button>
            <Button size="sm" variant="outline" onClick={() => setStage("idle")}>
              Cancel
            </Button>
            {countdown && <span className="text-xs text-muted-foreground tabular-nums">{countdown}</span>}
          </div>
        </div>
      )}
      {stage === "sending" && <p className="text-sm text-muted-foreground">Sending…</p>}
      <div aria-live="polite" className="text-sm">
        {answer && (
          <span className={answer.ok ? "text-positive" : "text-negative"}>{answerText(answer)}</span>
        )}
        {failure && <span className="text-negative">{failure}</span>}
      </div>
    </div>
  );
}
