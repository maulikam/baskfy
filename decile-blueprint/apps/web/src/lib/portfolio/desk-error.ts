import { DeskUnavailable } from "@/lib/desk/fetch";

/**
 * What a reader is told when the desk does not answer.
 *
 * **The transport's own message is never it.** `DeskUnavailable.message` is the fetch's text, and
 * for a 404 that reads `http://127.0.0.1:8100/api/v1/desk/regime responded 404` — an internal host
 * and port, on a page a customer opens. That shipped, and it was found by looking at a screenshot
 * rather than by any of 2,800 tests, which is why this is a module with a test rather than a line
 * inside a page.
 *
 * The detail is not thrown away. The caller logs the error; the person who can act on a 404 is
 * reading the logs, and the person reading the page can only act on "it did not answer".
 */
export function readerSafeDeskError(error: unknown): string {
  return error instanceof DeskUnavailable
    ? "The desk did not answer."
    : "The desk could not be reached.";
}
