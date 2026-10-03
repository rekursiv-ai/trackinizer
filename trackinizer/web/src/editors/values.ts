import type { QueryClient } from "@tanstack/react-query";
import type { FieldRoute } from "../api/fields";
import type { FieldLook } from "../detail/fields";

/** A list field's value as a list of strings; unset is empty. */
export function asList(value: unknown): readonly string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

/** One element added to or removed from a list, waiting to be sent. */
export type Toggle = { readonly op: "add" | "sub"; readonly value: string };

/**
 * `list` with each toggle applied in order, as the server will store it once
 * they have all landed. A list is a set: an add of an element it has, or a
 * remove of one it lacks, changes nothing. A `byline` keeps order and repeats:
 * an add appends, and a remove drops the first match only.
 */
export function withToggles(list: readonly string[], toggles: readonly Toggle[], byline = false): readonly string[] {
  return toggles.reduce((shown, { op, value }) => {
    if (op === "add") return byline || !shown.includes(value) ? [...shown, value] : shown;
    return byline ? shown.toSpliced(shown.indexOf(value), shown.includes(value) ? 1 : 0) : shown.filter((item) => item !== value);
  }, list);
}

/**
 * The list fields that are bylines, the server's `is_byline` columns: their
 * `PATCH` keeps order and repeats. The schema does not say which lists these
 * are, so the set cannot come from it.
 */
export const BYLINES: ReadonlySet<string> = new Set(["authors"]);

/**
 * Every distinct value of `field` on the rows already loaded, sorted: list pages
 * and details in the query cache. No route lists owners, subscribers or labels,
 * so pickers offer these, plus whatever the user types.
 */
export function loadedValues(queryClient: QueryClient, field: string): string[] {
  const values = new Set<string>();
  for (const query of queryClient.getQueryCache().getAll()) {
    for (const row of rowsIn(query.state.data)) {
      const value = row[field];
      for (const item of Array.isArray(value) ? value : [value]) {
        if (typeof item === "string" && item) values.add(item);
      }
    }
  }
  return [...values].sort((a, b) => a.localeCompare(b));
}

/** How a field's value is typed in a form: its input, and how its text reads back. */
export type InputKind = "text" | "integer" | "number" | "day" | "datetime" | "json";

/** The input for a field, from its value type in the field-type map and how the detail draws it. */
export function inputKind(route: FieldRoute, look: FieldLook): InputKind {
  const { type, format } = route.value;
  if (look.format === "day") return "day";
  if (format === "date-time") return "datetime";
  if (type === "integer" || type === "number") return type;
  return type === "string" ? "text" : "json";
}

/** A stored value as an input's text; unset is empty. */
export function inputText(value: unknown, kind: InputKind): string {
  if (value === undefined || value === null) return "";
  switch (kind) {
    case "day":
      // A calendar date is stored at midnight UTC, and shown as UTC (`calendarDate`).
      return new Date(String(value)).toISOString().slice(0, 10);
    case "datetime":
      return localDateTime(new Date(String(value)));
    case "json":
      return JSON.stringify(value, null, 2);
  }
  return String(value);
}

/**
 * An input's text as the value to save: `null` when empty, which clears the
 * field. Only what cannot be sent at all is refused here, such as text that is
 * not JSON; the server checks everything else and says what is wrong.
 */
export function parseInput(text: string, kind: InputKind): { readonly value: unknown } | { readonly error: string } {
  const trimmed = text.trim();
  if (!trimmed) return { value: null };
  switch (kind) {
    case "integer":
    case "number": {
      const number = Number(trimmed);
      return Number.isNaN(number) ? { error: "Enter a number." } : { value: number };
    }
    case "day":
      return { value: `${trimmed}T00:00:00+00:00` };
    case "datetime":
      // `datetime-local` text has no offset, so it is the browser's local time.
      return { value: new Date(trimmed).toISOString() };
    case "json":
      try {
        return { value: JSON.parse(trimmed) };
      } catch (error) {
        return { error: `Not valid JSON: ${error instanceof Error ? error.message : String(error)}` };
      }
  }
  return { value: trimmed };
}

/** Rows in a cached read: a list page's rows, or a detail's own row. Neighbours on edges are not rows. */
function rowsIn(data: unknown): readonly { readonly [field: string]: unknown }[] {
  if (Array.isArray(data)) return data.filter(isRow);
  if (isRecord(data) && isRow(data.self)) return [data.self];
  return [];
}

function isRow(value: unknown): value is { readonly [field: string]: unknown } {
  return isRecord(value) && typeof value.seq === "number" && typeof value.created === "string";
}

function isRecord(value: unknown): value is { readonly [key: string]: unknown } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `2026-09-27T14:05`, in the browser's time zone, as `datetime-local` takes it. */
function localDateTime(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
