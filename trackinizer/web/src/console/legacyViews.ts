import type { Preferences } from "../preferences";

/** A console view as the preferences keep it. */
export type KeptView = Preferences["console_views"][number];

/** The `localStorage` key a browser from before the views moved to the server kept them under. */
export function legacyViewsKey(email: string): string {
  return `trackinizer.v2.console.${email}`;
}

/**
 * The views in the text an older build stored. Text that is not JSON, and a view
 * of another shape, with a time that is not one, a range ending before it starts,
 * or past the server's bounds (`ConsoleView`), are left out: the server would
 * refuse the whole send for one of them.
 */
export function parseLegacyViews(text: string): KeptView[] {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return [];
  }
  return Array.isArray(value) ? value.filter(isView) : [];
}

function isView(value: unknown): value is KeptView {
  if (typeof value !== "object" || value === null) return false;
  const view = value as { readonly [field: string]: unknown };
  const range = view.range as { readonly [field: string]: unknown } | null | undefined;
  return (
    isText(view.id, 100) &&
    isText(view.name, 200) &&
    isTime(view.created) &&
    typeof view.pinned === "boolean" &&
    ["agents", "rooms", "clis"].every((field) => Array.isArray(view[field]) && view[field].length <= 100 && view[field].every((name) => isText(name, 200))) &&
    [1, 2, 3, 4].includes(view.level as number) &&
    typeof range === "object" &&
    range !== null &&
    (range.live === true || (range.live === false && isWindow(range.since, range.until)))
  );
}

/** Whether `since` and `until` bound a window: each a time or open (null), and the window not ending before it starts. */
function isWindow(since: unknown, until: unknown): boolean {
  if ((since !== null && !isTime(since)) || (until !== null && !isTime(until))) return false;
  return !isTime(since) || !isTime(until) || Date.parse(since) <= Date.parse(until);
}

function isText(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length <= max;
}

/** Whether `value` is a time `Date` reads, short enough for the server. */
function isTime(value: unknown): value is string {
  return isText(value, 40) && !Number.isNaN(Date.parse(value));
}
