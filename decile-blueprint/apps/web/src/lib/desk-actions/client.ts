/** Browser-side calls to the desk, same-origin `/desk` (Caddy proxy). Orders stay on the desk. */
export const DESK = "/desk";

export class DeskError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function readDetail(response: Response): Promise<string> {
  const text = await response.text();
  try {
    const body = JSON.parse(text) as { detail?: unknown; error?: unknown };
    if (typeof body.detail === "string" && body.detail) return body.detail;
    if (typeof body.error === "string" && body.error) return body.error;
    if (Array.isArray(body.detail) && body.detail[0] && typeof body.detail[0] === "object") {
      const first = body.detail[0] as { msg?: string };
      if (first.msg) return first.msg;
    }
  } catch {
    return text || response.statusText;
  }
  return text || response.statusText;
}

export async function deskGet<T>(path: string, params: Record<string, string> = {}): Promise<T> {
  const qs = new URLSearchParams(params).toString();
  const response = await fetch(`${DESK}${path}${qs ? `?${qs}` : ""}`, {
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new DeskError(response.status, await readDetail(response));
  return (await response.json()) as T;
}

export async function deskForm<T>(path: string, fields: Record<string, string>): Promise<T> {
  const response = await fetch(`${DESK}${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams(fields).toString(),
  });
  if (!response.ok) throw new DeskError(response.status, await readDetail(response));
  return (await response.json()) as T;
}
