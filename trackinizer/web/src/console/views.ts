import { useCallback, useMemo, useState } from "react";
import { newUuid } from "../api/idempotency";
import { choosePreferences, preferences, receivePreferences, usePreferences } from "../preferences";
import type { KeptView } from "./legacyViews";
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
 * The signed-in user's console views, in the rail's order, and the one open in
 * this tab.
 *
 * The views are the user's `console_views` preference, kept on the server
 * (`src/preferences.ts`) so any browser finds them, and every hook showing them
 * follows a change made in any tab. The open view is the stored one with this
 * tab's id, so another tab's edit shows here and its delete opens the first view
 * left. That id lives in this tab's `sessionStorage`, so a reload opens it again;
 * a tab with none opens the first view, or a fresh one. A fresh view ("Untitled view", every agent's
 * messages, live) is stored on its first change, as a new conversation is. When
 * the server refuses a change, the open view still shows it, unsaved.
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
  const { console_views: stored } = usePreferences();
  const views = useMemo(() => orderViews(stored), [stored]);
  const [openId, setOpenId] = useState(readOpenId);
  // The view this tab holds unstored: a fresh one, or a change the server refused.
  const [unsaved, setUnsaved] = useState(freshView);
  const open = views.find(({ id }) => id === openId) ?? (unsaved.id === openId ? unsaved : (views[0] ?? unsaved));
  const show = useCallback((id: string) => {
    setOpenId(id);
    try {
      sessionStorage.setItem(OPEN_KEY, id);
    } catch {
      // Storage is off: a reload opens the first view.
    }
  }, []);
  return {
    views,
    open,
    change: (view) => {
      // Shown as the stored one now; a later delete in any tab must not leave a copy here.
      setUnsaved(freshView());
      void store((all) => [...all.filter(({ id }) => id !== view.id), kept(view)]).then((saved) => {
        if (!saved) setUnsaved(view);
      });
      show(view.id);
    },
    openView: (id) => {
      const fresh = freshView();
      setUnsaved(fresh);
      show(id ?? fresh.id);
    },
    edit: (id, change) => {
      void store((all) => all.map((one) => (one.id === id ? kept(change(one)) : one)));
    },
    remove: (id) => {
      void store((all) => all.filter((one) => one.id !== id));
    },
  };
}

/**
 * Show and send `change` of the views the preferences hold now; whether the
 * server kept it. One it refused is taken back, so the rail lists what is kept.
 */
async function store(change: (views: readonly KeptView[]) => KeptView[]): Promise<boolean> {
  const before = preferences().console_views;
  if (await choosePreferences({ console_views: change(before) })) return true;
  receivePreferences({ ...preferences(), console_views: before });
  return false;
}

/** `view` as the preferences keep it: lists the page may not change are copied. */
function kept(view: View): KeptView {
  return { ...view, agents: [...view.agents], rooms: [...view.rooms], clis: [...view.clis] };
}

/** The id this tab kept in its `sessionStorage`, or null when there is none or storage is off. */
function readOpenId(): string | null {
  try {
    return sessionStorage.getItem(OPEN_KEY);
  } catch {
    return null;
  }
}

/** The tab's open view, by id. */
const OPEN_KEY = "trackinizer.v2.console.open";

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
