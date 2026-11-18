import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { HighlightContext, HighlightStore, useHighlighted, useIsHighlighted } from "./highlights";

afterEach(cleanup);

function Marked() {
  return <output data-testid="marks">{[...useHighlighted()].join(",")}</output>;
}

function Row({ id, onRender }: { id: string; onRender?: () => void }) {
  onRender?.();
  return <output data-testid={`row-${id}`}>{String(useIsHighlighted(id))}</output>;
}

test("the newest set of ids replaces the last, and an empty one clears", () => {
  const store = new HighlightStore();
  render(<HighlightContext value={store}><Marked /></HighlightContext>);
  expect(screen.getByTestId("marks").textContent).toBe("");
  act(() => store.set(["a", "b"]));
  expect(screen.getByTestId("marks").textContent).toBe("a,b");
  act(() => store.set(["c"]));
  expect(screen.getByTestId("marks").textContent).toBe("c");
  act(() => store.set([]));
  expect(screen.getByTestId("marks").textContent).toBe("");
});

test("outside a canvas nothing is highlighted and nothing breaks", () => {
  render(<><Marked /><Row id="a" /></>);
  expect(screen.getByTestId("marks").textContent).toBe("");
  expect(screen.getByTestId("row-a").textContent).toBe("false");
});

test("a row draws again only when its own mark changes", () => {
  const store = new HighlightStore();
  let renders = 0;
  render(<HighlightContext value={store}><Row id="a" onRender={() => renders++} /></HighlightContext>);
  const first = renders;
  act(() => store.set(["b"]));
  act(() => store.set(["b", "c"]));
  expect(renders).toBe(first);
  act(() => store.set(["a"]));
  expect(screen.getByTestId("row-a").textContent).toBe("true");
  act(() => store.set([]));
  expect(screen.getByTestId("row-a").textContent).toBe("false");
});

test("setting the same ids again tells no one", () => {
  const store = new HighlightStore();
  let told = 0;
  store.subscribe(() => told++);
  store.set(["a", "b"]);
  store.set(["b", "a", "a"]);
  store.set([]);
  store.set([]);
  expect(told).toBe(2);
});
