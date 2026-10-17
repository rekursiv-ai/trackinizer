import type { components } from "../api/generated/schema";
import type { Layout } from "./stationLayout";

/** One of a station's canvas tabs, as its layout keeps it (`server/visuals/workspaces.py`). */
export type CanvasTab = components["schemas"]["CanvasTab"];

/** The tab a station has before a second is made, which its layout does not name. */
const FIRST: CanvasTab = { id: "1", name: "Tab 1" };

/** A station's canvas tabs, in order: the one it has before a second is made, if its layout keeps none. */
export function tabsOf(layout: Layout): readonly CanvasTab[] {
  if (layout.tabs?.length) return layout.tabs;
  return layout.tabs ? [{ ...FIRST, id: layout.tab ?? FIRST.id }] : [FIRST];
}

/** The id of the open tab: the layout's, if it names one it keeps, else the first. */
export function openTabOf(layout: Layout): string {
  const tabs = tabsOf(layout);
  return tabs.some((tab) => tab.id === layout.tab) ? layout.tab! : tabs[0]!.id;
}

/** The item that takes `id`'s place when it closes: the one after it, or before it when it was last. */
export function neighbourOf<T extends { readonly id: string }>(tabs: readonly T[], id: string): T | null {
  const at = tabs.findIndex((tab) => tab.id === id);
  return tabs[at + 1] ?? tabs[at - 1] ?? null;
}
