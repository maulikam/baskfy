/** `GET /desk/ops` (Accept: application/json) — `ops::view` plus the page's collection block. */
export interface OpParam {
  key: string;
  label: string;
  kind: string;
  default: string | null;
  help: string;
}

export interface OpSpec {
  name: string;
  label: string;
  group: string;
  summary: string;
  timeout: number;
  params: OpParam[];
  needs_kite: boolean;
  writes: boolean;
  long_running: boolean;
}

export interface OpJob {
  id: number;
  name: string;
  argv_json: string;
  status: string;
  exit_code: number | null;
  started_at: string;
  finished_at: string | null;
  duration_s: number | null;
  output: string | null;
}

export interface OpEntry {
  op: OpSpec;
  scans: string[];
  last: OpJob | null;
  cli: string;
}

export interface Collection {
  ever_ran: boolean;
  healthy: boolean;
  consecutive_failures: number;
  message: string;
  ran_at?: string | null;
  trigger?: string | null;
  stale_days: number | null;
}

export interface DailyRun {
  ran_at: string;
  session_date: string;
  trigger: string;
  outcome: string;
  steps_json: string;
}

export interface OpsView {
  groups: string[];
  operations: OpEntry[];
  running: OpJob | null;
  history: OpJob[];
  dry_run: boolean;
  has_scan: boolean;
  collection: Collection;
  daily_history: DailyRun[];
}

export function argv(job: OpJob): string {
  try {
    const parts = JSON.parse(job.argv_json) as unknown;
    return Array.isArray(parts) ? `python ${parts.map(String).join(" ")}` : job.argv_json;
  } catch {
    return job.argv_json;
  }
}

export function failedSteps(run: DailyRun): string {
  try {
    const steps = JSON.parse(run.steps_json) as unknown;
    if (!Array.isArray(steps)) return "—";
    const bad = steps
      .filter((s): s is unknown[] => Array.isArray(s) && s[1] === "FAILED")
      .map((s) => String(s[0]));
    return bad.length ? bad.join(", ") : "—";
  } catch {
    return "—";
  }
}
