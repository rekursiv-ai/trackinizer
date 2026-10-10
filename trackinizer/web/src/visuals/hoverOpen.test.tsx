import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { FOLD_AFTER_MS, OPEN_AFTER_MS, useHoverOpen } from "./hoverOpen";

/** A tile as the canvas draws one: a bar with a fold button, and contents that take the keyboard. */
function Tile() {
  const tile = useRef<HTMLDivElement>(null);
  const hover = useHoverOpen(tile);
  return <div ref={tile} data-testid="tile" data-open={hover.open} {...hover.handlers}>
    <div className="visual-tile-toolbar"><button type="button" onClick={() => hover.set(!hover.open)}>Fold</button></div>
    <textarea aria-label="Message" />
  </div>;
}

function mount() {
  render(<><Tile /><button type="button">Page</button></>);
  const tile = screen.getByTestId("tile");
  return { tile, open: () => tile.dataset.open === "true", wait: (ms: number) => act(() => { vi.advanceTimersByTime(ms); }) };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test("the tile opens once the pointer has rested on it, and folds once it has been off it", () => {
  const { tile, open, wait } = mount();
  fireEvent.pointerEnter(tile);
  wait(OPEN_AFTER_MS - 1);
  expect(open()).toBe(false);
  wait(1);
  expect(open()).toBe(true);
  fireEvent.pointerLeave(tile);
  wait(FOLD_AFTER_MS - 1);
  expect(open()).toBe(true);
  wait(1);
  expect(open()).toBe(false);
});

test("a pointer only crossing the tile opens nothing, and one that comes back folds nothing", () => {
  const { tile, open, wait } = mount();
  fireEvent.pointerEnter(tile);
  fireEvent.pointerLeave(tile);
  wait(OPEN_AFTER_MS + FOLD_AFTER_MS);
  expect(open()).toBe(false);

  fireEvent.pointerEnter(tile);
  wait(OPEN_AFTER_MS);
  fireEvent.pointerLeave(tile);
  wait(FOLD_AFTER_MS - 1);
  fireEvent.pointerEnter(tile);
  wait(FOLD_AFTER_MS);
  expect(open()).toBe(true);
});

test("the keyboard in the contents holds the tile open after the pointer left, until it leaves too", () => {
  const { tile, open, wait } = mount();
  fireEvent.pointerEnter(tile);
  wait(OPEN_AFTER_MS);
  act(() => screen.getByLabelText("Message").focus());
  fireEvent.pointerLeave(tile);
  wait(FOLD_AFTER_MS);
  expect(open()).toBe(true);
  act(() => screen.getByRole("button", { name: "Page" }).focus());
  wait(FOLD_AFTER_MS);
  expect(open()).toBe(false);
});

test("a button on the bar folds the tile at once, takes the keyboard out of it, and never holds it open", () => {
  const { tile, open, wait } = mount();
  const fold = screen.getByRole("button", { name: "Fold" });
  fireEvent.click(fold);
  expect(open()).toBe(true);
  // The pointer is on the bar and the keyboard in the contents: both would hold it open.
  fireEvent.pointerEnter(tile);
  act(() => screen.getByLabelText("Message").focus());
  fireEvent.click(fold);
  expect(open()).toBe(false);
  expect(document.activeElement).toBe(document.body);
  wait(OPEN_AFTER_MS + FOLD_AFTER_MS);
  expect(open()).toBe(false);

  // Focus on the bar alone is not the keyboard in the contents.
  fireEvent.click(fold);
  act(() => fold.focus());
  fireEvent.pointerLeave(tile);
  wait(FOLD_AFTER_MS);
  expect(open()).toBe(false);
});

test("a touch has no hover: it opens and folds nothing by coming and going", () => {
  const { tile, open, wait } = mount();
  fireEvent.pointerEnter(tile, { pointerType: "touch" });
  wait(OPEN_AFTER_MS);
  expect(open()).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Fold" }));
  fireEvent.pointerLeave(tile, { pointerType: "touch" });
  wait(FOLD_AFTER_MS);
  expect(open()).toBe(true);
});
