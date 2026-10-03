import { cleanup, render } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { RESULTS, SearchResults, searchNodes } from "./search";
import { node } from "./testing";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const NODES = [
  node(1, { title: "Retry jitter" }),
  node(2, { title: "Cache budget", kind: "Paper" }),
  node(12, { title: "Retry backoff", kind: "Belief" }),
  node(21, { title: "Untitled" }),
];
const seqs = (query: string) => searchNodes(NODES, query).map((found) => found.seq);

test("a title or kind matches by substring, in any case, newest first", () => {
  expect(seqs("retry")).toEqual([12, 1]);
  expect(seqs("BUDGET")).toEqual([2]);
  expect(seqs("paper")).toEqual([2]);
  expect(seqs("bel")).toEqual([12]);
});

test("#seq or bare digits match a seq exactly, of any kind", () => {
  expect(seqs("#12")).toEqual([12]);
  expect(seqs("2")).toEqual([2]);
  expect(seqs("#1")).toEqual([1]);
});

test("blank asks nothing, and every match comes back, not the oldest 40 (G2)", () => {
  expect(seqs("  ")).toEqual([]);
  const many = Array.from({ length: 50 }, (_, n) => node(n + 1, { title: "Same" }));
  expect(searchNodes(many, "same").map((found) => found.seq)).toEqual(many.map((row) => row.seq).toReversed());
});

test("the marked match scrolls into view as the keys move it past what the list shows", () => {
  // jsdom has none.
  Element.prototype.scrollIntoView = () => {};
  const scrolled = vi.spyOn(Element.prototype, "scrollIntoView");
  const rows = Array.from({ length: 40 }, (_, n) => node(n + 1, { title: "Same" }));
  const panel = { ...RESULTS, collapsed: false, setCollapsed: () => {}, toggle: () => {}, button: { current: null }, refocus: { current: false } };
  const results = (marked: number) =>
    createElement(SearchResults, { id: "m", query: "same", total: 40, groups: [{ name: null, rows }], marked, hopsOf: () => undefined, onPick: () => {}, onFocus: () => {}, panel });
  const { rerender } = render(results(-1));
  expect(scrolled).not.toHaveBeenCalled();
  rerender(results(35));
  expect(scrolled.mock.contexts.map((row) => (row as Element).id)).toEqual(["m-35"]);
  expect(scrolled).toHaveBeenLastCalledWith({ block: "nearest" });
});
