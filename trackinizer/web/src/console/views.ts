import { useCallback, useMemo, useState, useSyncExternalStore } from "react";
import { newUuid } from "../api/idempotency";
import { useProfile } from "../app/boot";
import type { Selection } from "./facets";
import type { Range } from "./feed";
import type { Level } from "./levels";

/**
 * A console view: what it picks (agents and rooms by name or pattern, CLIs),
 * its level and its place in time, under a name. It saves itself as it
 * changes; pinned views list first.
 */
export type View = Selection & {
  readonly id: string;
  readonly name: string;
  readonly pinned: boolean;
  /** When it was started, ISO; views list newest first. */
  readonly created: string;
  readonly level: Level;
  readonly range: Range;
};

/** The views in the rail's order: pinned first, then the rest, each newest first. */
export function orderViews(views: readonly View[]): View[] {
  return views.toSorted((a, b) => Number(b.pinned) - Number(a.pinned) || b.created.localeCompare(a.created));
}

/**
 * The signed-in user's console views in this browser, in the rail's order, and
 * the one open in this tab.
 *
 * The views live in `localStorage` under the user's email, as drafts do, and
 * every hook showing them follows a change made in any tab. The open view is
 * the stored one with this tab's id, so another tab's edit shows here and its
 * delete opens the first view left. That id lives in this tab's
 * `sessionStorage`, so a reload opens it again; a tab with none opens the first
 * view, or a fresh one. A fresh view ("Untitled view", every agent's messages,
 * live) is stored on its first change, as a new conversation is. When the
 * browser refuses to store, or denies storage altogether, the open view still
 * changes, unsaved.
 */
export function useViews(): {
  views: readonly View[];
  open: View;
  /** The open view changed: show and store it. */
  change: (view: View) => void;
  /** Open view `id`, or a fresh one. */
  openView: (id: string | null) => void;
  /** Store a change to view `id`: a rename or a pin. */
  edit: (id: string, change: (view: View) => View) => void;
  remove: (id: string) => void;
} {
  const { email } = useProfile();
  const key = `trackinizer.v2.console.${email}`;
  const stored = useSyncExternalStore(subscribe, () => read("localStorage", key));
  const views = useMemo(() => orderViews(parseViews(stored)), [stored]);
  const [openId, setOpenId] = useState(() => read("sessionStorage", OPEN_KEY));
  // The view this tab holds unstored: a fresh one, or a change the browser refused.
  const [unsaved, setUnsaved] = useState(freshView);
  const open = views.find(({ id }) => id === openId) ?? (unsaved.id === openId ? unsaved : (views[0] ?? unsaved));
  const show = useCallback((id: string) => {
    setOpenId(id);
    write("sessionStorage", OPEN_KEY, id);
  }, []);
  /** Store `change` of the stored views; whether the browser stored it. */
  const store = useCallback(
    (change: (views: readonly View[]) => readonly View[]): boolean => {
      const saved = write("localStorage", key, JSON.stringify(change(parseViews(read("localStorage", key)))));
      // A `storage` event reaches other tabs only; this tells this one's hooks.
      dispatchEvent(new Event(CHANGED));
      return saved;
    },
    [key],
  );
  return {
    views,
    open,
    change: (view) => {
      const saved = store((all) => [...all.filter(({ id }) => id !== view.id), view]);
      // Saved, the view is the stored one; a later delete in any tab must not leave a copy here.
      setUnsaved(saved ? freshView() : view);
      show(view.id);
    },
    openView: (id) => {
      const fresh = freshView();
      setUnsaved(fresh);
      show(id ?? fresh.id);
    },
    edit: (id, change) => {
      store((all) => all.map((one) => (one.id === id ? change(one) : one)));
    },
    remove: (id) => {
      store((all) => all.filter((one) => one.id !== id));
    },
  };
}

/** The tab's open view, by id. */
const OPEN_KEY = "trackinizer.v2.console.open";

/** Fired on `window` when this tab stores views. */
const CHANGED = "trackinizer-console-views";

function freshView(): View {
  return {
    id: newUuid(),
    name: "Untitled view",
    pinned: false,
    created: new Date().toISOString(),
    agents: [],
    rooms: [],
    clis: [],
    level: 1,
    range: { live: true },
  };
}

function subscribe(onChange: () => void): () => void {
  addEventListener("storage", onChange);
  addEventListener(CHANGED, onChange);
  return () => {
    removeEventListener("storage", onChange);
    removeEventListener(CHANGED, onChange);
  };
}

/** Which of the browser's storage areas; named, since a denied area throws as soon as it is reached. */
type Area = "localStorage" | "sessionStorage";

/** The stored text, or null when there is none or storage is off. */
function read(area: Area, key: string): string | null {
  try {
    return window[area].getItem(key);
  } catch {
    return null;
  }
}

/** Store `text`; whether the browser stored it. */
function write(area: Area, key: string, text: string): boolean {
  try {
    window[area].setItem(key, text);
    return true;
  } catch {
    // Storage off or full: the open view still shows the change, unsaved.
    return false;
  }
}

/**
 * The stored views. Text that is not JSON, and a view of another shape (written
 * by another build, or by hand), its times not times or its range ending before
 * it starts, are left out rather than stopping the console.
 */
function parseViews(text: string | null): View[] {
  let value: unknown;
  try {
    value = JSON.parse(text ?? "[]");
  } catch {
    return [];
  }
  return Array.isArray(value) ? value.filter(isView) : [];
}

function isView(value: unknown): value is View {
  if (typeof value !== "object" || value === null) return false;
  const view = value as { readonly [field: string]: unknown };
  const range = view.range as { readonly [field: string]: unknown } | null | undefined;
  return (
    ["id", "name"].every((field) => typeof view[field] === "string") &&
    isTime(view.created) &&
    typeof view.pinned === "boolean" &&
    ["agents", "rooms", "clis"].every((field) => Array.isArray(view[field]) && view[field].every((name) => typeof name === "string")) &&
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

/** Whether `value` is a time `Date` reads. */
function isTime(value: unknown): value is string {
  return typeof value === "string" && !Number.isNaN(Date.parse(value));
}
