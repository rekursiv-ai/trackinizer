/**
 * The app's one logger: key events into a bounded ring, and onto the console.
 *
 * Warnings and errors always reach the console; info and debug only while debug
 * is on (`?debug=1`, or the `localStorage` switch; see `install.ts`). Every event
 * goes into the ring whatever its level, so Copy details can show what led up
 * to a failure, as Sentry's breadcrumbs do.
 *
 * An event's fields are ids, kinds, fields, routes, statuses and timings. Never
 * pass request bodies, header values, token secrets, or text a user wrote
 * (titles, descriptions, search text): the ring is copied into bug reports.
 */

/** How much an event matters. */
export type Level = "debug" | "info" | "warn" | "error";

/** An event's fields; see the module comment for what may go in them. */
export type Fields = { readonly [name: string]: string | number | boolean | null | undefined };

/** One logged event. `at` is an ISO time. */
export type LogEntry = { readonly at: string; readonly level: Level; readonly event: string; readonly fields: Fields };

/**
 * Log `event` at `level`: into the ring, and onto the console when the level is
 * warn or error, or debug is on.
 */
export function log(level: Level, event: string, fields: Fields = {}): void {
  const entry: LogEntry = { at: new Date().toISOString(), level, event, fields };
  ring.push(entry);
  if (ring.length > RING_SIZE) ring.shift();
  if (level === "warn" || level === "error" || debug.on) console[level](`trackinizer ${formatEvent(event, fields)}`);
}

/** The ring's events, oldest first. */
export function recentEvents(): readonly LogEntry[] {
  return [...ring];
}

/** Whether info and debug events reach the console. */
export function setDebug(on: boolean): void {
  debug.on = on;
}

/**
 * One event as one logfmt line: `event key=value …`, the shape the server's own
 * request lines have, so a request id greps the same in both. Values with spaces
 * or quotes are quoted; unset fields are left out.
 */
export function formatEvent(event: string, fields: Fields): string {
  const pairs = Object.entries(fields)
    .filter(([, value]) => value !== undefined && value !== null)
    .map(([name, value]) => `${name}=${typeof value === "string" && /[\s"=]|^$/.test(value) ? JSON.stringify(value) : value}`);
  return [event, ...pairs].join(" ");
}

/**
 * How many events the ring keeps. Sentry keeps 100 breadcrumbs by default; the
 * live stream's refetches log a request each, a few a second, so this keeps the
 * minute or so before a failure.
 */
const RING_SIZE = 200;

const ring: LogEntry[] = [];
const debug = { on: false };
