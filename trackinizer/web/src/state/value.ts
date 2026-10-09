import { newUuid } from "../api/idempotency";
import type { Filter, FilterOp, ListRequest } from "../query/query";

/**
 * This browser's own state for one signed-in user: what trackinizer does not
 * store. One versioned JSON value in `localStorage` (`store.ts`); Settings
 * exports it, and imports another browser's with `mergeImport`.
 */
export type BrowserState = {
  readonly version: typeof VERSION;
  /** Starred inquiries, by id: one person's bookmarks, never a label. */
  readonly stars: readonly string[];
  readonly views: readonly SavedView[];
  /**
   * The names besides the account email that mean me in owner and subscriber
   * filters. The user ticks them in Settings; none is added automatically.
   */
  readonly aliases: readonly string[];
  /** Names for people and agents added by hand, by the value that goes in owner or subscribers. */
  readonly people: { readonly [actor: string]: Person };
  readonly read: ReadState;
  /** How this browser shows things; an import leaves it as it is. */
  readonly ui: UiState;
};

/** A saved view: its name, and exactly the `GET /api/inquiries` query it shows. */
export type SavedView = { readonly id: string; readonly name: string; readonly request: ListRequest };

/** A person or an agent added by hand, under the name the pickers show. */
export type Person = { readonly name: string; readonly type: "person" | "agent" };

/** Which notifications are read. */
export type ReadState = {
  /** Changes at or before this time (ISO 8601) count as read; null until Notifications first loads. */
  readonly boundary: string | null;
  /** Changes after the boundary marked read one at a time, by change id. */
  readonly marks: readonly string[];
};

/** Collapsed sections, by key, the detail lens last used, and how each floating tile was left. */
export type UiState = {
  readonly collapsed: readonly string[];
  readonly lens: string | null;
  /** Floating canvas tiles, by visual type. */
  readonly tiles: { readonly [type: string]: TileMemory };
};

/**
 * How a floating tile was left: folded to its top bar or not, and where the
 * user dragged it, in pixels from the canvas stage's top left (null until it
 * was dragged).
 */
export type TileMemory = {
  readonly collapsed: boolean;
  readonly place: { readonly left: number; readonly top: number } | null;
};

/**
 * The value's version. A change to its shape bumps it, and `parseState` refuses
 * a newer one. It reads every version from `OLDEST_READABLE`, filling in what
 * the older ones lack, so the next store writes the current shape.
 *
 * - 2: `ui.tiles`.
 */
export const VERSION = 2;

/** The oldest version `parseState` reads. */
const OLDEST_READABLE = 1;

/** The state of a browser that has stored nothing yet. */
export const EMPTY_STATE: BrowserState = {
  version: VERSION,
  stars: [],
  views: [],
  aliases: [],
  people: {},
  read: { boundary: null, marks: [] },
  ui: { collapsed: [], lens: null, tiles: {} },
};

/**
 * `value`, a parsed export or stored value, as a `BrowserState`.
 *
 * A category it lacks is empty, so a hand-written file of stars alone imports.
 * Anything of the wrong shape throws an `Error` whose message says what is wrong.
 */
export function parseState(value: unknown): BrowserState {
  if (!isRecord(value) || typeof value.version !== "number") {
    throw new Error("This is not an export of Trackinizer's browser state.");
  }
  if (value.version > VERSION || value.version < OLDEST_READABLE) {
    throw new Error(`This export is version ${value.version}; this build reads version ${VERSION}. Reload for the latest build.`);
  }
  const read = record(value.read ?? {}, "read");
  const ui = record(value.ui ?? {}, "ui");
  return {
    version: VERSION,
    stars: strings(value.stars ?? [], "stars"),
    views: list(value.views ?? [], "views").map(savedView),
    aliases: strings(value.aliases ?? [], "aliases"),
    people: Object.fromEntries(Object.entries(record(value.people ?? {}, "people")).map(([actor, person]) => [actor, personOf(person, actor)])),
    read: { boundary: boundaryOf(read.boundary ?? null), marks: strings(read.marks ?? [], "read.marks") },
    ui: {
      collapsed: strings(ui.collapsed ?? [], "ui.collapsed"),
      lens: nullableString(ui.lens ?? null, "ui.lens"),
      tiles: Object.fromEntries(
        Object.entries(record(ui.tiles ?? {}, "ui.tiles")).map(([type, tile]) => [type, tileOf(tile, type)]),
      ),
    },
  };
}

/** `state` as the text of an export file. */
export function exportState(state: BrowserState): string {
  return `${JSON.stringify(state, null, 2)}\n`;
}

/**
 * Merge an imported state into this browser's. Nothing here is deleted:
 *
 * - stars, aliases and people are unions; a person already named here keeps
 *   this browser's name;
 * - views merge by id: an identical view is skipped, and one whose id is taken
 *   by different content is added as a copy with a new id (`newId`) and
 *   "(imported)" after its name, unless that copy is already here, so
 *   importing one file twice adds it once;
 * - read state takes the later of the two boundaries and the union of the marks;
 * - UI state stays this browser's.
 */
export function mergeImport(
  current: BrowserState,
  imported: BrowserState,
  newId: () => string = newUuid,
): BrowserState {
  return {
    ...current,
    stars: union(current.stars, imported.stars),
    views: mergeViews(current.views, imported.views, newId),
    aliases: union(current.aliases, imported.aliases),
    people: { ...imported.people, ...current.people },
    read: {
      boundary: later(current.read.boundary, imported.read.boundary),
      marks: union(current.read.marks, imported.read.marks),
    },
  };
}

function mergeViews(current: readonly SavedView[], imported: readonly SavedView[], newId: () => string): SavedView[] {
  const merged = [...current];
  for (const view of imported) {
    const holder = merged.find((kept) => kept.id === view.id);
    if (holder === undefined) {
      merged.push(view);
      continue;
    }
    if (sameJson(holder, view)) continue;
    const copy = { ...view, name: `${view.name} (imported)` };
    if (merged.some((kept) => sameJson({ ...kept, id: "" }, { ...copy, id: "" }))) continue;
    merged.push({ ...copy, id: newId() });
  }
  return merged;
}

/** `a` then whatever of `b` it lacks, each once. */
function union(a: readonly string[], b: readonly string[]): string[] {
  return [...new Set([...a, ...b])];
}

/** The later of two times; either may be unset. */
function later(a: string | null, b: string | null): string | null {
  if (a === null || b === null) return a ?? b;
  return Date.parse(b) > Date.parse(a) ? b : a;
}

/** Whether two JSON values are equal, whatever order their keys are in. */
function sameJson(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (!isRecord(a) || !isRecord(b) || Array.isArray(a) !== Array.isArray(b)) return false;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every((key) => Object.hasOwn(b, key) && sameJson(a[key], b[key]));
}

function savedView(value: unknown, index: number): SavedView {
  const where = `views[${index}]`;
  const view = record(value, where);
  const request = record(view.request, `${where}.request`);
  return {
    id: text(view.id, `${where}.id`),
    name: text(view.name, `${where}.name`),
    request: {
      kinds: strings(request.kinds, `${where}.request.kinds`),
      filters: list(request.filters, `${where}.request.filters`).map((filter, at) => filterOf(filter, `${where}.request.filters[${at}]`)),
    },
  };
}

function filterOf(value: unknown, where: string): Filter {
  const filter = record(value, where);
  const op = text(filter.op, `${where}.op`);
  if (!isFilterOp(op)) throw new Error(`${where}.op must be one of ${Object.keys(FILTER_OPS).join(", ")}.`);
  return { field: text(filter.field, `${where}.field`), op, value: text(filter.value, `${where}.value`) };
}

/** A read boundary: a time `Date.parse` reads, since the merge keeps the later of two. */
function boundaryOf(value: unknown): string | null {
  const boundary = nullableString(value, "read.boundary");
  if (boundary !== null && Number.isNaN(Date.parse(boundary))) {
    throw new Error("read.boundary must be a time, such as 2026-09-27T10:00:00Z.");
  }
  return boundary;
}

function personOf(value: unknown, actor: string): Person {
  const where = `people.${actor}`;
  const person = record(value, where);
  const type = text(person.type, `${where}.type`);
  if (type !== "person" && type !== "agent") throw new Error(`${where}.type must be person or agent.`);
  return { name: text(person.name, `${where}.name`), type };
}

function tileOf(value: unknown, type: string): TileMemory {
  const where = `ui.tiles.${type}`;
  const tile = record(value, where);
  if (typeof tile.collapsed !== "boolean") throw new Error(`${where}.collapsed must be true or false.`);
  if ((tile.place ?? null) === null) return { collapsed: tile.collapsed, place: null };
  const place = record(tile.place, `${where}.place`);
  return { collapsed: tile.collapsed, place: { left: pixels(place.left, `${where}.place.left`), top: pixels(place.top, `${where}.place.top`) } };
}

function pixels(value: unknown, where: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${where} must be a number.`);
  return value;
}

/** Every filter operator, so an imported one is checked; `satisfies` keeps it equal to `FilterOp`. */
const FILTER_OPS = {
  is: true,
  ne: true,
  re: true,
  nre: true,
  lt: true,
  le: true,
  gt: true,
  ge: true,
  isnull: true,
  notnull: true,
} as const satisfies { readonly [op in FilterOp]: true };

function isFilterOp(op: string): op is FilterOp {
  return Object.hasOwn(FILTER_OPS, op);
}

function isRecord(value: unknown): value is { readonly [key: string]: unknown } {
  return typeof value === "object" && value !== null;
}

function record(value: unknown, where: string): { readonly [key: string]: unknown } {
  if (!isRecord(value) || Array.isArray(value)) throw new Error(`${where} must be an object.`);
  return value;
}

function list(value: unknown, where: string): readonly unknown[] {
  if (!Array.isArray(value)) throw new Error(`${where} must be a list.`);
  return value;
}

function strings(value: unknown, where: string): string[] {
  return list(value, where).map((item, index) => text(item, `${where}[${index}]`));
}

function text(value: unknown, where: string): string {
  if (typeof value !== "string") throw new Error(`${where} must be text.`);
  return value;
}

function nullableString(value: unknown, where: string): string | null {
  return value === null ? null : text(value, where);
}
