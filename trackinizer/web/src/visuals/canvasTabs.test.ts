import { expect, test } from "vitest";
import { type CanvasTab, neighbourOf, openTabOf, tabsOf } from "./canvasTabs";

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

test("closing a tab opens the one after it, or before it when it was last", () => {
  expect(neighbourOf(tabs, "a")?.id).toBe("b");
  expect(neighbourOf(tabs, "c")?.id).toBe("b");
  expect(neighbourOf([tabs[0]!], "a")).toBeNull();
});
