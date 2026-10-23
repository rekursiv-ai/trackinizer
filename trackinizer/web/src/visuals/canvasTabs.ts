import type { components } from "../api/generated/schema";
import type { Snapshot } from "./canvasSnapshot";
import { type Layout, layoutOf } from "./canvasLayout";

/** One of a canvas's canvas tabs, as its layout keeps it (`server/visuals/canvases.py`). */
export type CanvasTab = components["schemas"]["CanvasTab"];

/** The most tabs a canvas keeps. */
export const MAX_TABS = 12;

/** The tab a canvas has before a second is made, which its layout does not name. */
const FIRST: CanvasTab = { id: "1", name: "Tab 1" };

/** A canvas's canvas tabs, in order: the one it has before a second is made, if its layout keeps none. */
export function tabsOf(layout: Layout): readonly CanvasTab[] {
  if (layout.tabs?.length) return layout.tabs;
  return layout.tabs ? [{ ...FIRST, id: layout.tab ?? FIRST.id }] : [FIRST];
}

/** The id of the open tab: the layout's, if it names one it keeps, else the first. */
export function openTabOf(layout: Layout): string {
  const tabs = tabsOf(layout);
  return tabs.some((tab) => tab.id === layout.tab) ? layout.tab! : tabs[0]!.id;
}

/** The name of a new tab: `Tab N`, for the lowest N no tab is named. */
export function newTabName(tabs: readonly CanvasTab[]): string {
  const taken = new Set(tabs.map((tab) => tab.name));
  let n = 1;
  while (taken.has(`Tab ${n}`)) n += 1;
  return `Tab ${n}`;
}

/** The item that takes `id`'s place when it closes: the one after it, or before it when it was last. */
export function neighbourOf<T extends { readonly id: string }>(tabs: readonly T[], id: string): T | null {
  const at = tabs.findIndex((tab) => tab.id === id);
  return tabs[at + 1] ?? tabs[at - 1] ?? null;
}

/** What tab `tab` shows, to open it again. */
export function tabSnapshot(tab: CanvasTab): Snapshot {
  return {
    page: tab.page ?? null,
    visuals: (tab.visuals ?? []).map((visual) => ({
      type: visual.type,
      placement: visual.placement ?? "main",
      record_id: visual.record_id ?? null,
      params: visual.params ?? {},
    })),
  };
}

/**
 * Tab `tab` as it is left: what it shows (`snapshot`) and the sizes its
 * columns and windows have (`layout`'s), kept in its place in the layout.
 */
export function leftTab(tab: CanvasTab, snapshot: Snapshot, layout: Layout): CanvasTab {
  return {
    id: tab.id,
    name: tab.name,
    page: snapshot.page,
    visuals: snapshot.visuals.map((visual) => ({ ...visual, params: { ...visual.params } })),
    ...(layout.columns ? { columns: layout.columns } : {}),
    ...(layout.floating ? { floating: layout.floating } : {}),
    ...(layout.tiles ? { tiles: layout.tiles } : {}),
    ...(layout.share ? { share: layout.share } : {}),
  };
}

/** The layout's sizes as tab `tab` left them, each one it never set emptied: a canvas's sizes are its open tab's. */
export function sizesOf(tab: CanvasTab | null): Layout {
  return layoutOf({ columns: tab?.columns ?? {}, floating: tab?.floating ?? {}, tiles: tab?.tiles ?? {}, share: tab?.share ?? {} });
}
