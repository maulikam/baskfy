import { AlertTriangle, CircleSlash, PlugZap, ServerCrash } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import type { DataStatus, ProblemBody, ProviderStatus } from "@/lib/opportunities/types";

function Panel({
  tone,
  icon,
  title,
  children,
  testId,
}: {
  tone: "warning" | "negative" | "neutral";
  icon: ReactNode;
  title: string;
  children?: ReactNode;
  testId: string;
}) {
  const toneClass =
    tone === "warning"
      ? "border-warning/50 bg-warning-muted text-foreground"
      : tone === "negative"
        ? "border-negative/50 bg-negative-muted text-foreground"
        : "border-border bg-muted/50 text-foreground";
  return (
    <div role="status" data-testid={testId} className={`flex gap-3 rounded-md border p-3 text-sm ${toneClass}`}>
      <span className="mt-0.5 shrink-0 [&_svg]:size-4">{icon}</span>
      <div className="min-w-0 space-y-1">
        <p className="font-medium">{title}</p>
        {children ? <div className="text-muted-foreground">{children}</div> : null}
      </div>
    </div>
  );
}

export function DataStatusBanner({
  status,
  reason,
}: {
  status: DataStatus;
  reason: string | null;
}) {
  if (status === "FRESH") return null;
  if (status === "STALE") {
    return (
      <Panel tone="warning" icon={<AlertTriangle />} title="Data is stale — nothing here is actionable" testId="stale-banner">
        {reason ?? "The latest session has not been published yet."}
      </Panel>
    );
  }
  return (
    <Panel tone="negative" icon={<CircleSlash />} title="Data unavailable" testId="unavailable-banner">
      {reason ?? "The data this scan needs could not be read."}
    </Panel>
  );
}

export function WarningsList({ warnings }: { warnings: readonly string[] }) {
  if (warnings.length === 0) return null;
  return (
    <Panel tone="warning" icon={<AlertTriangle />} title="Partial result" testId="warnings">
      <ul className="list-disc space-y-0.5 pl-4">
        {warnings.map((warning) => (
          <li key={warning}>{warning}</li>
        ))}
      </ul>
    </Panel>
  );
}

export function ErrorPanel({ problem, onRetry }: { problem: ProblemBody; onRetry?: () => void }) {
  return (
    <Panel
      tone="negative"
      icon={<ServerCrash />}
      title={problem.status > 0 ? `${problem.title} (HTTP ${problem.status})` : problem.title}
      testId="http-error"
    >
      {problem.detail ? <p>{problem.detail}</p> : null}
      {onRetry ? (
        <Button variant="outline" size="sm" className="mt-2" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </Panel>
  );
}

export function ProviderUnavailable({ provider }: { provider: ProviderStatus }) {
  return (
    <Panel tone="neutral" icon={<PlugZap />} title="Options data provider unavailable" testId="provider-unavailable">
      {provider.reason ? <p>{provider.reason}</p> : null}
      {provider.dependency ? (
        <p>
          Needs: <code className="font-mono text-xs">{provider.dependency}</code>
        </p>
      ) : null}
    </Panel>
  );
}

export function ProgressBar({ percent, label }: { percent: number; label: string }) {
  return (
    <div className="space-y-1" data-testid="scan-progress">
      <div
        role="progressbar"
        aria-label="Scan progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
      >
        <div className="h-full bg-accent transition-[width] duration-200" style={{ width: `${percent}%` }} />
      </div>
      <p className="text-xs text-muted-foreground">{label}</p>
    </div>
  );
}
