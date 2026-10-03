import type { FieldOwners } from "../api/meta";

/** An operator `GET /api/inquiries` filters with, and `trax` spells the same way. */
export type FilterOp = "is" | "ne" | "re" | "nre" | "lt" | "le" | "gt" | "ge" | "isnull" | "notnull";

/**
 * One filter as the list route takes it, a JSON `{field, op, value}`. Every filter
 * in a request ANDs with the others. `value` is `""` for `isnull` and `notnull`.
 */
export type Filter = { readonly field: string; readonly op: FilterOp; readonly value: string };

/** Which statuses a list shows: `active`, anything else, or every status. */
export type Tab = "active" | "closed" | "all";

/** A field the Filter menu offers. `kind` narrows the kinds instead of filtering rows. */
export type ChoiceField = "kind" | "status" | "priority" | "judgement" | "owner" | "labels";

/**
 * What is ticked for one field in the Filter menu. Values of one field OR together.
 *
 * For `priority` the values are bands (`"0"` for P0 up to `"3"`), always one
 * contiguous range, or `NO_PRIORITY` alone: filters AND together, so an OR of
 * separate ranges has no exact query (COLD-07).
 */
export type Choice = { readonly field: ChoiceField; readonly values: readonly string[] };

/**
 * What a list shows: the one query object.
 *
 * The screen shows it (tabs and filter chips), `compileQuery` turns it into the
 * request, and `traxLine` into the matching command, so the three never disagree
 * (COLD-05, COLD-06). Grouping and sorting are not part of it: they rearrange
 * loaded rows and never reach the server.
 */
export type ListQuery = {
  /** The kinds the list covers, PascalCase. */
  readonly kinds: readonly string[];
  readonly tab: Tab;
  /** In the order they were added, at most one per field. */
  readonly choices: readonly Choice[];
};

/** A query as `GET /api/inquiries` takes it; saving a view keeps exactly this. */
export type ListRequest = {
  /** The kinds to ask for: never one that lacks a filtered field (S8). Empty asks nothing. */
  readonly kinds: readonly string[];
  readonly filters: readonly Filter[];
};

/** The `priority` value for rows with none. */
export const NO_PRIORITY = "none";

/**
 * The request `query` makes, for the user whose names are `me`: the account
 * email first, then the aliases ticked in Settings.
 *
 * `owner` is free text, and most rows name their owner by a handle, not the
 * email, so the email ticked as an owner (the Filter menu's Me) stands for every
 * one of `me` (the plan's "Who me is").
 */
export function compileQuery(query: ListQuery, fieldOwners: FieldOwners, me: readonly string[] = []): ListRequest {
  const filters = [...tabFilters(query.tab), ...query.choices.flatMap((choice) => choiceFilters(choice, me))];
  const only = query.choices.find((choice) => choice.field === "kind")?.values;
  const kinds = query.kinds.filter(
    (kind) =>
      (!only || only.includes(kind)) &&
      filters.every((filter) => appliesTo(filter.field, kind, fieldOwners)),
  );
  return { kinds, filters };
}

/**
 * Whether rows of `kind` have `field`: a base column, which every kind has, or
 * one `/api/meta/fields` gives to that kind. The server answers 400 for the
 * whole request when any requested kind lacks a filtered field (S8).
 */
export function appliesTo(field: string, kind: string, fieldOwners: FieldOwners): boolean {
  const owner = fieldOwners[field];
  return owner === undefined || owner === kind.toLowerCase();
}

/**
 * The fields the Filter menu offers for `query`: a kind's own field when at
 * least one of the list's kinds has it, Kind when there are several, and Status
 * unless the Active tab already decides it.
 */
export function filterFields(query: ListQuery, fieldOwners: FieldOwners): ChoiceField[] {
  const some = (field: string) => query.kinds.some((kind) => appliesTo(field, kind, fieldOwners));
  const offered: [ChoiceField, boolean][] = [
    ["kind", query.kinds.length > 1],
    ["status", query.tab !== "active"],
    ["priority", some("priority")],
    ["judgement", some("judgement")],
    ["owner", true],
    ["labels", true],
  ];
  return offered.filter(([, shown]) => shown).map(([field]) => field);
}

/**
 * The `trax` command that lists what `request` does, as a line any shell runs
 * as shown. A value that starts with `-` follows a `--`, which ends trax's
 * flags; quoting cannot help there, since the shell strips the quotes.
 */
export function traxLine(request: ListRequest): string {
  const kinds = request.kinds.map((kind) => kind.toLowerCase());
  const filters = request.filters.flatMap(({ field, op, value }) =>
    value === "" ? [field, op] : [field, op, shellWord(value)],
  );
  const flagLike = request.filters.some(({ value }) => value.startsWith("-"));
  return ["trax", ...kinds, ...(flagLike ? ["--"] : []), ...filters].join(" ");
}

/**
 * A filter matching any of `values` exactly: `is` for one value, else one
 * anchored `re` over the escaped values (COLD-08). On a list field such as
 * `labels`, both match rows where any element matches.
 */
export function oneOf(field: string, values: readonly string[]): Filter {
  if (values.length === 1) return { field, op: "is", value: values[0]! };
  return { field, op: "re", value: `^(${values.map(escapeRegex).join("|")})$` };
}

/**
 * `literal` as a regex matching only itself. Every character escaped is
 * punctuation, which a backslash makes literal in both Postgres and Python.
 */
export function escapeRegex(literal: string): string {
  return literal.replace(/[\\^$.*+?()[\]{}|]/g, "\\$&");
}

/** Whether `value` is a list tab, as a stored one read back may not be. */
export function isListTab(value: unknown): value is Tab {
  return value === "active" || value === "closed" || value === "all";
}

/**
 * Whether `value` is choices as the Filter menu makes them: known fields, each
 * once, with values, and priority bands one contiguous range or No priority
 * alone. A stored query read back may be none of these, and `compileQuery`
 * refuses a priority that is not one range.
 */
export function isChoices(value: unknown): value is readonly Choice[] {
  if (!Array.isArray(value)) return false;
  const fields = value.map((choice: { field?: unknown }) => choice?.field);
  return (
    new Set(fields).size === fields.length &&
    value.every(
      (choice: { field?: unknown; values?: unknown }) =>
        CHOICE_FIELDS.some((field) => field === choice.field) &&
        Array.isArray(choice.values) &&
        choice.values.length > 0 &&
        choice.values.every((v: unknown) => typeof v === "string") &&
        (choice.field !== "priority" || isPriorityRange(choice.values as string[])),
    )
  );
}

/** Tick or untick `value` for `field`; a choice left empty goes. */
export function toggleChoice(query: ListQuery, field: ChoiceField, value: string): ListQuery {
  const current = query.choices.find((choice) => choice.field === field)?.values ?? [];
  const values =
    field === "priority"
      ? togglePriority(current, value)
      : current.includes(value)
        ? current.filter((v) => v !== value)
        : [...current, value];
  const choices = current.length
    ? query.choices.map((choice) => (choice.field === field ? { field, values } : choice))
    : [...query.choices, { field, values }];
  return { ...query, choices: choices.filter((choice) => choice.values.length > 0) };
}

/** Drop the choice for `field`. */
export function removeChoice(query: ListQuery, field: ChoiceField): ListQuery {
  return { ...query, choices: query.choices.filter((choice) => choice.field !== field) };
}

/** Switch to `tab`. A status choice goes with the old tab, since the tab decides status. */
export function withTab(query: ListQuery, tab: Tab): ListQuery {
  return { ...removeChoice(query, "status"), tab };
}

function tabFilters(tab: Tab): Filter[] {
  if (tab === "active") return [{ field: "status", op: "is", value: "active" }];
  if (tab === "closed") return [{ field: "status", op: "ne", value: "active" }];
  return [];
}

function choiceFilters({ field, values }: Choice, me: readonly string[]): Filter[] {
  if (field === "kind") return [];
  if (field === "owner" && me.length && values.includes(me[0]!)) {
    return [oneOf(field, [...new Set(values.flatMap((value) => (value === me[0] ? me : [value])))])];
  }
  if (field !== "priority") return [oneOf(field, values)];
  if (values.includes(NO_PRIORITY)) return [{ field, op: "isnull", value: "" }];
  const bands = values.map(Number);
  const low = Math.min(...bands);
  const high = Math.max(...bands);
  assertRange(bands, low, high);
  // P3 has no upper bound, so backlog (40) and anything lower still match Low.
  const below: Filter[] = high < 3 ? [{ field, op: "lt", value: String((high + 1) * 10) }] : [];
  return [{ field, op: "ge", value: String(low * 10) }, ...below];
}

/**
 * The bands ticked after clicking `value`: a range stays one range. Outside it,
 * the range grows to reach the band; at an end, that end goes; in the middle,
 * the band alone is left.
 */
function togglePriority(current: readonly string[], value: string): string[] {
  if (value === NO_PRIORITY) return current.includes(NO_PRIORITY) ? [] : [NO_PRIORITY];
  const band = Number(value);
  const bands = current.filter((v) => v !== NO_PRIORITY).map(Number);
  if (bands.length === 0) return [value];
  let low = Math.min(...bands);
  let high = Math.max(...bands);
  if (band < low) low = band;
  else if (band > high) high = band;
  else if (band === low && band === high) return [];
  else if (band === low) low += 1;
  else if (band === high) high -= 1;
  else low = high = band;
  return Array.from({ length: high - low + 1 }, (_, k) => String(low + k));
}

const CHOICE_FIELDS: readonly ChoiceField[] = ["kind", "status", "priority", "judgement", "owner", "labels"];

/** Whether `values` are what a priority choice holds: No priority alone, or one range of bands. */
function isPriorityRange(values: readonly string[]): boolean {
  if (values.includes(NO_PRIORITY)) return values.length === 1;
  const bands = values.map(Number);
  if (!bands.every((band) => Number.isInteger(band) && band >= 0 && band <= 3)) return false;
  const distinct = new Set(bands).size;
  return distinct === bands.length && distinct === Math.max(...bands) - Math.min(...bands) + 1;
}

function assertRange(bands: readonly number[], low: number, high: number): void {
  if (new Set(bands).size !== high - low + 1) {
    throw new Error(`Priority bands ${bands.join(", ")} are not one range.`);
  }
}

/**
 * `value` as one shell word: bare when no shell reads any of it specially, else
 * single-quoted. `#` is never bare: it starts a comment in bash and is a glob in
 * zsh with EXTENDED_GLOB; nor is a leading `=`, which zsh expands to a command's
 * path.
 */
function shellWord(value: string): string {
  return /^(?!=)[\w.@:/+=,-]+$/.test(value) ? value : `'${value.replace(/'/g, `'\\''`)}'`;
}
