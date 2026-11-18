import { expect, test } from "vitest";
import { homeSnapshot } from "./canvasSnapshot";
import { type CanvasTab, leftTab, neighbourOf, newTabName, openTabOf, sizesOf, tabSnapshot, tabsOf } from "./canvasTabs";

const tabs: CanvasTab[] = [{ id: "a", name: "Tab 1" }, { id: "b", name: "Review" }, { id: "c", name: "Tab 3" }];

test("a station keeping no tabs has one, open", () => {
  expect(tabsOf({})).toEqual([{ id: "1", name: "Tab 1" }]);
  expect(openTabOf({})).toBe("1");
  expect(openTabOf({ tab: "gone" })).toBe("1");
});

test("an explicitly empty tab list keeps its selected tab as Tab 1", () => {
  expect(tabsOf({ tabs: [], tab: "b" })).toEqual([{ id: "b", name: "Tab 1" }]);
  expect(openTabOf({ tabs: [], tab: "b" })).toBe("b");
});

test("the open tab is the one the layout names, if it keeps it", () => {
  expect(openTabOf({ tabs, tab: "b" })).toBe("b");
  expect(openTabOf({ tabs, tab: "gone" })).toBe("a");
});

test("a new tab takes the lowest free number", () => {
  expect(newTabName(tabs)).toBe("Tab 2");
  expect(newTabName([...tabs, { id: "d", name: "Tab 2" }])).toBe("Tab 4");
});

test("closing a tab opens the one after it, or before it when it was last", () => {
  expect(neighbourOf(tabs, "a")?.id).toBe("b");
  expect(neighbourOf(tabs, "c")?.id).toBe("b");
  expect(neighbourOf([tabs[0]!], "a")).toBeNull();
});

test("a tab left keeps what it showed and its sizes, and opens again to the same", () => {
  const shown = { page: "#/ref/Issue/7", visuals: [{ type: "trax.subgraph", placement: "side" as const, record_id: "r1", params: { hops: 2 } }] };
  const left = leftTab(tabs[1]!, shown, { columns: { side: 0.3 }, share: { "trax.browse": 1.5 }, chat_ch: 80 });
  expect(left).toEqual({ id: "b", name: "Review", page: "#/ref/Issue/7", visuals: shown.visuals, columns: { side: 0.3 }, share: { "trax.browse": 1.5 } });
  expect(tabSnapshot(left)).toEqual(shown);
  expect(sizesOf(left)).toEqual({ columns: { side: 0.3 }, floating: {}, tiles: {}, share: { "trax.browse": 1.5 } });
});

test("a tab as the server sends it, its unset keys null, opens with nothing missing", () => {
  const stored: CanvasTab = { id: "x", name: "Tab 1", page: null, visuals: [{ type: "trax.browse" }], columns: null, floating: null, tiles: null, share: null };
  expect(tabSnapshot(stored)).toEqual({ page: null, visuals: [{ type: "trax.browse", placement: "main", record_id: null, params: {} }] });
  expect(sizesOf(stored)).toEqual({ columns: {}, floating: {}, tiles: {}, share: {} });
  expect(tabSnapshot({ id: "y", name: "Home" })).toEqual(homeSnapshot("empty"));
});
