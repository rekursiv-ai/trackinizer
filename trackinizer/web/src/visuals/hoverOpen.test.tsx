import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { FOLD_AFTER_MS, OPEN_AFTER_MS, RAIL_FOLD_AFTER_MS, useHoverOpen } from "./hoverOpen";

/** A tile that folds on its own: a fold button, a button beside it, and contents that take the keyboard. */
function Tile({ enabled = true, foldAfterMs }: { readonly enabled?: boolean; readonly foldAfterMs?: number }) {
  const tile = useRef<HTMLDivElement>(null);
  const hover = useHoverOpen(tile, { enabled, foldAfterMs });
  return <div ref={tile} data-testid="tile" data-open={hover.open}>
    <button type="button" onClick={() => hover.set(!hover.open)}>Fold</button>
    <textarea aria-label="Message" />
  </div>;
}

function mount(enabled = true, foldAfterMs?: number) {
  render(<><Tile enabled={enabled} foldAfterMs={foldAfterMs} /><button type="button">Page</button></>);
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
  fireEvent.pointerMove(tile);
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

test("a tile given its own fold wait folds that long after the pointer left, and after the keyboard left", () => {
  expect(RAIL_FOLD_AFTER_MS).toBeLessThan(FOLD_AFTER_MS);
  const { tile, open, wait } = mount(true, RAIL_FOLD_AFTER_MS);
  fireEvent.pointerMove(tile);
  wait(OPEN_AFTER_MS);
  fireEvent.pointerLeave(tile);
  wait(RAIL_FOLD_AFTER_MS - 1);
  expect(open()).toBe(true);
  wait(1);
  expect(open()).toBe(false);
  act(() => screen.getByRole("textbox", { name: "Message" }).focus());
  wait(OPEN_AFTER_MS);
  act(() => screen.getByRole("button", { name: "Page" }).focus());
  wait(RAIL_FOLD_AFTER_MS);
  expect(open()).toBe(false);
});

test("a tile that comes to lie under a resting pointer, as when the page loads under it, opens once the pointer moves", () => {
  const { tile, open, wait } = mount();
  fireEvent.pointerEnter(tile);
  fireEvent.pointerOver(tile);
  wait(OPEN_AFTER_MS + FOLD_AFTER_MS);
  expect(open()).toBe(false);
  fireEvent.pointerMove(screen.getByLabelText("Message"));
  wait(OPEN_AFTER_MS);
  expect(open()).toBe(true);
});

test("a pointer only crossing the tile opens nothing, and one that comes back folds nothing", () => {
  const { tile, open, wait } = mount();
  fireEvent.pointerMove(tile);
  fireEvent.pointerLeave(tile);
  wait(OPEN_AFTER_MS + FOLD_AFTER_MS);
  expect(open()).toBe(false);

  fireEvent.pointerMove(tile);
  wait(OPEN_AFTER_MS);
  fireEvent.pointerLeave(tile);
  wait(FOLD_AFTER_MS - 1);
  fireEvent.pointerMove(tile);
  wait(FOLD_AFTER_MS);
  expect(open()).toBe(true);
});

test("the keyboard in the contents holds the tile open after the pointer left, until it leaves too", () => {
  const { tile, open, wait } = mount();
  fireEvent.pointerMove(tile);
  wait(OPEN_AFTER_MS);
  act(() => screen.getByLabelText("Message").focus());
  fireEvent.pointerLeave(tile);
  wait(FOLD_AFTER_MS);
  expect(open()).toBe(true);
  act(() => screen.getByRole("button", { name: "Page" }).focus());
  wait(FOLD_AFTER_MS);
  expect(open()).toBe(false);
});

test("set folds the tile at once and takes the keyboard out of it, and a fold under the pointer stays folded", () => {
  const { tile, open, wait } = mount();
  const fold = screen.getByRole("button", { name: "Fold" });
  fireEvent.click(fold);
  expect(open()).toBe(true);
  // The pointer is on the tile and the keyboard in its contents: both would hold it open.
  fireEvent.pointerMove(tile);
  act(() => screen.getByLabelText("Message").focus());
  fireEvent.click(fold);
  expect(open()).toBe(false);
  expect(document.activeElement).toBe(document.body);
  wait(OPEN_AFTER_MS + FOLD_AFTER_MS);
  expect(open()).toBe(false);
});

test("a touch has no hover: it opens and folds nothing by coming and going", () => {
  const { tile, open, wait } = mount();
  fireEvent.pointerMove(tile, { pointerType: "touch" });
  wait(OPEN_AFTER_MS);
  expect(open()).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Fold" }));
  fireEvent.pointerLeave(tile, { pointerType: "touch" });
  wait(FOLD_AFTER_MS);
  expect(open()).toBe(true);
});

test("a tile not watched opens nothing under the pointer", () => {
  const { tile, open, wait } = mount(false);
  fireEvent.pointerMove(tile);
  wait(OPEN_AFTER_MS + FOLD_AFTER_MS);
  expect(open()).toBe(false);
});
