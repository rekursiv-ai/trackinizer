import { afterEach, expect, test, vi } from "vitest";
import { watchRows } from "./visibility";

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

/** A list element holding rows `ids`. */
function list(...ids: string[]): HTMLElement {
  const root = document.createElement("div");
  for (const id of ids) root.append(row(id));
  document.body.append(root);
  return root;
}

function row(id: string): HTMLElement {
  const element = document.createElement("a");
  element.dataset.row = id;
  return element;
}

/** Let `MutationObserver` callbacks run. */
const mutations = () => new Promise((resolve) => setTimeout(resolve, 0));

test("without IntersectionObserver, rendered rows are on screen, and a row that appears enters", async () => {
  const root = list("a", "b");
  const entered: string[][] = [];
  const watch = watchRows(root, (ids) => entered.push(ids));
  expect([...watch.onScreen(["a", "c"])]).toEqual(["a"]);
  root.append(row("c"));
  await mutations();
  expect(entered).toEqual([["c"]]);
  expect([...watch.onScreen(["a", "c"])]).toEqual(["a", "c"]);
  watch.dispose();
  root.append(row("d"));
  await mutations();
  expect(entered).toHaveLength(1);
});

test("with IntersectionObserver, rows on screen are those intersecting, and rows scrolled in enter", async () => {
  const observers: FakeIntersection[] = [];
  class FakeIntersection {
    readonly observed = new Set<Element>();
    readonly callback: (entries: { target: Element; isIntersecting: boolean }[]) => void;
    constructor(callback: FakeIntersection["callback"]) {
      this.callback = callback;
      observers.push(this);
    }
    observe(element: Element) {
      this.observed.add(element);
    }
    unobserve(element: Element) {
      this.observed.delete(element);
    }
    disconnect() {
      this.observed.clear();
    }
    /** Report `ids`' rows as in view or not, as scrolling would. */
    report(ids: string[], isIntersecting: boolean) {
      this.callback(
        [...this.observed]
          .filter((element) => ids.includes((element as HTMLElement).dataset.row!))
          .map((target) => ({ target, isIntersecting })),
      );
    }
  }
  vi.stubGlobal("IntersectionObserver", FakeIntersection);
  const root = list("a", "b", "c");
  const entered: string[][] = [];
  const watch = watchRows(root, (ids) => entered.push(ids));
  const [intersection] = observers;
  expect(intersection!.observed.size).toBe(3);
  intersection!.report(["a", "b"], true);
  expect(entered).toEqual([["a", "b"]]);
  expect([...watch.onScreen(["a", "b", "c"])]).toEqual(["a", "b"]);
  intersection!.report(["a"], false);
  intersection!.report(["c"], true);
  expect(entered).toEqual([["a", "b"], ["c"]]);
  expect([...watch.onScreen(["a", "b", "c"])]).toEqual(["b", "c"]);
  // A row removed stops being watched, and a new one starts.
  root.querySelector('[data-row="b"]')!.remove();
  root.append(row("d"));
  await mutations();
  expect([...intersection!.observed].map((element) => (element as HTMLElement).dataset.row)).toEqual(["a", "c", "d"]);
  expect([...watch.onScreen(["b"])]).toEqual([]);
});
