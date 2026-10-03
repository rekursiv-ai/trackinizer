import { ApiError, failureFields } from "../api/client";
import { formatEvent, recentEvents } from "./log";

/**
 * The text Copy details puts on the clipboard for a failure the UI shows: the
 * message shown, when and where, the build, the failed request (its id finds the
 * server's log line), and the ring's recent events.
 *
 * `error` is what failed: an `ApiError`, any other error (its stack is kept), or
 * null when only the message and the events tell it (a bulk edit's report).
 */
export function errorDetails(message: string, error: unknown): string {
  return [
    `Trackinizer web app: ${message}`,
    `time: ${new Date().toISOString()}`,
    `page: ${pageRoute()}`,
    `build: ${__COMMIT__}`,
    `browser: ${navigator.userAgent}`,
    ...failureLines(error),
    "recent events:",
    ...recentEvents().map(({ at, level, event, fields }) => `${at} ${level} ${formatEvent(event, fields)}`),
  ].join("\n");
}

/**
 * The page's route: the hash, less search text, which a user typed. A hash's
 * query (the old UI's `#/search?q=`) goes too.
 */
export function pageRoute(): string {
  return location.hash.replace(/\?.*$/, "").replace(/^(#\/search\/).+$/, "$1…") || "#/";
}

/** What `error` says about itself. */
function failureLines(error: unknown): string[] {
  if (error instanceof ApiError) return [`failed: ${formatEvent("request", { ...failureFields(error), at: error.sent?.at })}`];
  if (error instanceof Error) return [`error: ${error.name}: ${error.message}`, ...(error.stack ? [`stack: ${error.stack}`] : [])];
  return error === null || error === undefined ? [] : [`error: ${String(error)}`];
}

/**
 * The details a browser's clipboard last refused, until the console asks for
 * them (`trackinizer.details()`). They are kept, not logged: the message shown
 * can hold what a user wrote, which nothing logs.
 */
let refused: string | null = null;

/** Keep `text`, the details Copy details could not copy. */
export function keepRefused(text: string): void {
  refused = text;
}

/** The details the clipboard last refused, if it has refused any. */
export function refusedDetails(): string | null {
  return refused;
}
