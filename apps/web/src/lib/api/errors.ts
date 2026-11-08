import { isProblem, type ProblemOut } from "@baskfy/api-client";

/**
 * A thrown API failure that still carries its RFC 9457 problem document.
 *
 * TanStack Query rejects with whatever the query function throws, and throwing a bare object
 * loses the stack and trips every "throw an Error" lint there is. Wrapping keeps both: `Error`
 * semantics for the runtime, and `problem` for `ErrorState`, which renders the server's own
 * `title`/`detail` rather than inventing a message (docs/07 §"Error catalogue").
 */
export class ApiError extends Error {
  readonly problem: ProblemOut | null;
  readonly status: number | undefined;

  constructor(message: string, problem: ProblemOut | null, status?: number) {
    super(message);
    this.name = "ApiError";
    this.problem = problem;
    this.status = status;
  }

  static from(error: unknown, fallback: string, status?: number): ApiError {
    if (isProblem(error)) return new ApiError(error.detail || fallback, error, error.status);
    return new ApiError(fallback, null, status);
  }
}

/** The problem document behind an error, however it was wrapped. */
export function problemOf(error: unknown): ProblemOut | null {
  if (error instanceof ApiError) return error.problem;
  return isProblem(error) ? error : null;
}
