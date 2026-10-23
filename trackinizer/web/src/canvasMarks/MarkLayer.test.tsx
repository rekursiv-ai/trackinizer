import { readFileSync } from "node:fs";
import { join } from "node:path";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { clearMarks, drawMark, type Mark, removeMark, type CanvasState } from "../api/canvases";
import { createQueryClient } from "../app/queryClient";
import { CommandRegistry, CommandRegistryContext, Shortcuts, useCommands } from "../commands/registry";
import { MarkLayer } from "./MarkLayer";
import { useTileBodies } from "./tileBodies";
import { useBoxes } from "./useBoxes";

vi.mock("../api/canvases", () => ({ drawMark: vi.fn(), removeMark: vi.fn(), clearMarks: vi.fn() }));

// Read from disk: Vitest serves a stylesheet, `?raw` included, as an empty string.
const marksCss = readFileSync(join(import.meta.dirname, "marks.css"), "utf8");

const REC = "889ffcb2-cf44-43e7-9806-eb08428c6203";

const box = (id: string, page: string | null, tile?: string): Mark => ({
  id, seq: 1, author: "jp@x", page, at: "2026-10-17T00:00:00Z",
  points: [{ x: 0.1, y: 0.1 }, { x: 0.5, y: 0.5 }],
  ...(tile ? { tile } : {}),
});

const canvas = (marks: Mark[]): CanvasState => ({
  id: "w", name: "s", conversation_id: null, page: null, revision: 1, focused_instance: null, visuals: [], marks,
});

/** What the canvas does: the bar's two buttons and the layer, over one stage holding a tile, a record row and the bodies of `tiles`. */
function Harness({ canvas, tiles }: { readonly canvas: CanvasState; readonly tiles: readonly string[] }) {
  const boxes = useBoxes(canvas);
  const { bodies, bodyRef } = useTileBodies();
  return (
    <div data-testid="stage">
      <button type="button" onClick={boxes.toggle} aria-pressed={boxes.drawing}>pen</button>
      <button type="button" onClick={boxes.clearAll}>clear</button>
      <div data-visual-instance="inst-1" />
      <div data-row={REC} />
      {tiles.map((id) => <div key={id} data-visual-instance={id}><div className="visual-tile-body" data-testid={`body-${id}`} ref={bodyRef(id)} /></div>)}
      <MarkLayer boxes={boxes} bodies={bodies} />
    </div>
  );
}

/** A page's own Escape, as a record page's `detail.back` binds it, mounted before the canvas's pen. */
function Page({ back }: { readonly back: () => void }) {
  useCommands([{ id: "detail.back", title: "Back", keys: ["Escape"], run: back }]);
  return null;
}

function mount(marks: Mark[] = [], back: () => void = () => {}, places: Readonly<Record<string, readonly [number, number, number, number]>> = {}) {
  render(
    <QueryClientProvider client={createQueryClient(() => {})}>
      <CommandRegistryContext value={new CommandRegistry()}>
        <Shortcuts />
        <Page back={back} />
        <Harness canvas={canvas(marks)} tiles={Object.keys(places)} />
      </CommandRegistryContext>
    </QueryClientProvider>,
  );
  const rect = { left: 0, top: 0, width: 1000, height: 500, right: 1000, bottom: 500, x: 0, y: 0, toJSON: () => ({}) };
  // The tiles' bodies stand where `places` says (left, top, width, height); everything else fills the stage.
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const at = places[this.parentElement?.dataset.visualInstance ?? ""];
    if (!this.classList.contains("visual-tile-body") || !at) return rect;
    return { left: at[0], top: at[1], width: at[2], height: at[3], right: at[0] + at[2], bottom: at[1] + at[3], x: at[0], y: at[1], toJSON: () => ({}) };
  });
}

beforeEach(() => {
  location.hash = "#/graph";
  vi.mocked(drawMark).mockResolvedValue(canvas([]));
  vi.mocked(removeMark).mockResolvedValue(canvas([]));
  vi.mocked(clearMarks).mockResolvedValue(canvas([]));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks(); location.hash = ""; });

const pen = () => screen.getByRole("button", { name: "pen" });
const drag = (target: Element, path: readonly (readonly [number, number])[]) => {
  fireEvent.pointerDown(target, { pointerId: 1, button: 0, clientX: path[0]![0], clientY: path[0]![1] });
  for (const [x, y] of path.slice(1)) fireEvent.pointerMove(target, { pointerId: 1, clientX: x, clientY: y });
  fireEvent.pointerUp(target, { pointerId: 1 });
};
const shown = () => new Set([...document.querySelectorAll(".mark-box")].map((each) => each.getAttribute("data-mark")));

test("with the pen down nothing catches a click, so it reaches the page; the pen picks it up", () => {
  mount();
  expect(document.querySelector(".mark-capture")).toBeNull();
  expect(pen().getAttribute("aria-pressed")).toBe("false");
  fireEvent.click(pen());
  expect(document.querySelector(".mark-capture")).not.toBeNull();
  expect(pen().getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(pen());
  expect(document.querySelector(".mark-capture")).toBeNull();
});

test("Escape puts the pen down", async () => {
  mount();
  fireEvent.click(pen());
  await userEvent.setup().keyboard("{Escape}");
  expect(document.querySelector(".mark-capture")).toBeNull();
  expect(pen().getAttribute("aria-pressed")).toBe("false");
});

test("Escape with the pen down does not also run the page's own Escape, and with it up the page's runs", async () => {
  const back = vi.fn();
  mount([], back);
  const user = userEvent.setup();
  fireEvent.click(pen());
  await user.keyboard("{Escape}");
  expect(pen().getAttribute("aria-pressed")).toBe("false");
  expect(back).not.toHaveBeenCalled();
  await user.keyboard("{Escape}");
  expect(back).toHaveBeenCalledOnce();
});

test("a drag draws one box on this page with what lies under it", async () => {
  mount();
  fireEvent.click(pen());
  drag(document.querySelector(".mark-capture")!, [[100, 100], [300, 200], [400, 300]]);
  await waitFor(() => expect(drawMark).toHaveBeenCalledTimes(1));
  expect(vi.mocked(drawMark).mock.calls[0]).toEqual(["w", {
    points: [{ x: 0.1, y: 0.2 }, { x: 0.4, y: 0.6 }],
    page: "#/graph",
    covers: { instances: ["inst-1"], records: [REC] },
  }]);
});

test("a click with no drag draws no box", () => {
  mount();
  fireEvent.click(pen());
  drag(document.querySelector(".mark-capture")!, [[100, 100]]);
  expect(drawMark).not.toHaveBeenCalled();
});

test("a box shows on the page it was drawn on, hides when the page moves, and returns with it", async () => {
  mount([box("a", "#/graph"), box("b", "#/list/Issue"), box("c", null)]);
  expect(shown()).toEqual(new Set(["a", "c"]));
  await act(async () => { location.hash = "#/list/Issue"; });
  expect(shown()).toEqual(new Set(["b", "c"]));
  await act(async () => { location.hash = "#/activity"; });
  expect(shown()).toEqual(new Set(["c"]));
  await act(async () => { location.hash = "#/graph"; });
  expect(shown()).toEqual(new Set(["a", "c"]));
});

test("a sheet over the page shows no boxes and puts the pen down", async () => {
  mount([box("a", "#/graph")]);
  fireEvent.click(pen());
  await act(async () => { location.hash = "#/settings"; });
  expect(shown()).toEqual(new Set());
  expect(document.querySelector(".mark-capture")).toBeNull();
});

test("clicking a box's outline removes that box by its id", async () => {
  mount([box("a", "#/graph"), box("b", "#/graph")]);
  fireEvent.click(document.querySelector('[data-mark="b"] .mark-hit')!);
  await waitFor(() => expect(removeMark).toHaveBeenCalledWith("w", "b"));
  expect(removeMark).toHaveBeenCalledTimes(1);
});

test("a box's outline can be removed from the keyboard", async () => {
  mount([box("a", "#/graph")]);
  fireEvent.keyDown(screen.getByRole("button", { name: "Remove box" }), { key: "Enter" });
  await waitFor(() => expect(removeMark).toHaveBeenCalledWith("w", "a"));
});

test("only the outline takes a click: the inside and the layer pass it through", () => {
  mount([box("a", "#/graph")]);
  // jsdom lays nothing out, so what takes a click is read from the style sheet's rules.
  const rule = (selector: string) => {
    const start = `\n${marksCss}`.indexOf(`\n${selector} {`);
    return start < 0 ? "" : `\n${marksCss}`.slice(start).split("}")[0]!;
  };
  expect(rule(".mark-layer")).toContain("pointer-events: none");
  expect(rule(".mark-boxes")).toContain("pointer-events: none");
  expect(rule(".mark-box, .mark-draft")).toContain("pointer-events: none");
  expect(rule(".mark-outline")).toContain("fill: none");
  expect(rule(".mark-hit")).toContain("pointer-events: stroke");
  expect(rule(".mark-hit")).toContain("fill: none");
  expect(rule(".mark-hit")).toMatch(/stroke-width: (1[0-9]|[2-9][0-9])/);
  // And the visible outline carries no click of its own.
  fireEvent.click(document.querySelector('[data-mark="a"] .mark-outline')!);
  expect(removeMark).not.toHaveBeenCalled();
});

test("clear all takes the boxes of this page, as the page is when it is pressed", async () => {
  mount([box("a", "#/graph")]);
  fireEvent.click(screen.getByRole("button", { name: "clear" }));
  await waitFor(() => expect(clearMarks).toHaveBeenCalledWith("w", "#/graph"));
  expect(clearMarks).toHaveBeenCalledTimes(1);
});

test("a refused write says so and keeps the pen in hand", async () => {
  vi.mocked(drawMark).mockRejectedValue(new Error("no"));
  mount();
  fireEvent.click(pen());
  drag(document.querySelector(".mark-capture")!, [[100, 100], [400, 300]]);
  expect((await screen.findByRole("alert")).textContent).toBe("Could not save that box.");
  expect(document.querySelector(".mark-capture")).not.toBeNull();
});

const BODY = "body-t1";
/** The left, top, width and height of the tile `t1`'s body on the 1000 x 500 stage. */
const T1 = [500, 100, 400, 200] as const;
const styleOf = (element: Element) => {
  const { left, top, width, height } = (element as SVGElement).style;
  return { left, top, width, height };
};

test("a drag over a tile pins the box to it, in fractions of the tile's body", async () => {
  mount([], () => {}, { t1: T1 });
  fireEvent.click(pen());
  drag(document.querySelector(".mark-capture")!, [[550, 150], [850, 250]]);
  await waitFor(() => expect(drawMark).toHaveBeenCalledTimes(1));
  expect(vi.mocked(drawMark).mock.calls[0]![1]).toMatchObject({
    tile: "t1",
    points: [{ x: 0.125, y: 0.25 }, { x: 0.875, y: 0.75 }],
    page: "#/graph",
  });
});

test("the tile is the one that holds the box's center, so a box mostly over it but centered outside is the stage's", async () => {
  mount([], () => {}, { t1: T1 });
  fireEvent.click(pen());
  drag(document.querySelector(".mark-capture")!, [[100, 100], [550, 150]]);
  await waitFor(() => expect(drawMark).toHaveBeenCalledTimes(1));
  const drawn = vi.mocked(drawMark).mock.calls[0]![1];
  expect(drawn).not.toHaveProperty("tile");
  expect(drawn.points).toEqual([{ x: 0.1, y: 0.2 }, { x: 0.55, y: 0.3 }]);
});

test("a pinned box stands inside its tile's body in percentages, and not in the stage's layer", () => {
  mount([box("p", "#/graph", "t1")], () => {}, { t1: T1 });
  const pinned = screen.getByTestId(BODY).querySelector('[data-mark="p"]')!;
  expect(styleOf(pinned)).toEqual({ left: "30%", top: "30%", width: "40%", height: "40%" });
  expect(document.querySelector(".mark-boxes [data-mark]")).toBeNull();
});

test("a box with no tile stands in the stage's layer, in percentages of the stage", () => {
  mount([box("a", "#/graph")], () => {}, { t1: T1 });
  const loose = document.querySelector('.mark-boxes [data-mark="a"]')!;
  expect(styleOf(loose)).toEqual({ left: "30%", top: "30%", width: "40%", height: "40%" });
  expect(screen.getByTestId(BODY).querySelector("[data-mark]")).toBeNull();
});

test("a box whose tile is not on the canvas shows nowhere, and is not deleted", () => {
  mount([box("a", "#/graph", "gone")], () => {}, { t1: T1 });
  expect(shown()).toEqual(new Set());
  expect(removeMark).not.toHaveBeenCalled();
});

test("a pinned box's outline removes it as an unpinned one's does", async () => {
  mount([box("p", "#/graph", "t1")], () => {}, { t1: T1 });
  fireEvent.click(document.querySelector('[data-mark="p"] .mark-hit')!);
  await waitFor(() => expect(removeMark).toHaveBeenCalledWith("w", "p"));
});

test("a box shows at least 24 px wide and tall, grown evenly around the center it was drawn at", () => {
  // jsdom lays nothing out, so the floor is read from the style sheet's rule.
  const start = `\n${marksCss}`.indexOf("\n.mark-box, .mark-draft {");
  const rule = `\n${marksCss}`.slice(start).split("}")[0]!;
  expect(rule).toContain("min-width: 24px");
  expect(rule).toContain("min-height: 24px");
  expect(rule).toContain("transform: translate(-50%, -50%)");
  mount([box("a", "#/graph")]);
  // The box is placed by its center, which is what the transform grows it around.
  expect(styleOf(document.querySelector('[data-mark="a"]')!)).toMatchObject({ left: "30%", top: "30%" });
});
