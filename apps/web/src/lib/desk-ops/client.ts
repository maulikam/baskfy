import { DESK, DeskError, deskForm, deskGet } from "@/lib/desk-actions/client";

/** A desk form handler's JSON outcome (`Accept: application/json`); a refusal arrives as a DeskError. */
export interface DeskOutcome {
  ok: boolean;
  message: string;
}

export function postDesk(path: string, fields: Record<string, string>): Promise<DeskOutcome> {
  return deskForm<DeskOutcome>(path, fields);
}

/** A desk page's template context as JSON; an HTML answer means the desk predates the JSON branch. */
export async function readDeskPage<T>(path: string, params: Record<string, string> = {}): Promise<T> {
  try {
    return await deskGet<T>(path, params);
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new DeskError(
        502,
        "The desk answered with HTML, not JSON. Rebuild the local desk image so it serves this page's data.",
      );
    }
    throw error;
  }
}

/** Multipart POST (file upload) to the desk, same-origin. */
export async function postDeskFile(path: string, field: string, file: File): Promise<DeskOutcome> {
  const body = new FormData();
  body.append(field, file, file.name);
  const response = await fetch(`${DESK}${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    body,
  });
  const text = await response.text();
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    parsed = null;
  }
  if (!response.ok) {
    const detail =
      parsed && typeof parsed === "object" && "detail" in parsed && typeof parsed.detail === "string"
        ? parsed.detail
        : text || response.statusText;
    throw new DeskError(response.status, detail);
  }
  return (parsed as DeskOutcome | null) ?? { ok: true, message: "done" };
}

export function errorText(error: unknown): string {
  if (error instanceof DeskError) return error.message || `The desk answered ${error.status}.`;
  if (error instanceof Error) return error.message;
  return "The desk could not be reached.";
}
