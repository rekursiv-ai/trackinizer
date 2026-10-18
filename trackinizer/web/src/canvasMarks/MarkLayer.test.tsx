import { readFileSync } from "node:fs";
import { join } from "node:path";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { clearMarks, drawMark, type Mark, removeMark, type WorkspaceState } from "../api/workspaces";
import { createQueryClient } from "../app/queryClient";
import { CommandRegistry, CommandRegistryContext, Shortcuts, useCommands } from "../commands/registry";
import { MarkLayer } from "./MarkLayer";
import { useBoxes } from "./useBoxes";

vi.mock("../api/workspaces", () => ({ drawMark: vi.fn(), removeMark: vi.fn(), clearMarks: vi.fn() }));

// Read from disk: Vitest serves a stylesheet, `?raw` included, as an empty string.
const marksCss = readFileSync(join(import.meta.dirname, "marks.css"), "utf8");

const REC = "889ffcb2-cf44-43e7-9806-eb08428c6203";

const box = (id: string, page: string | null): Mark => ({
  id, seq: 1, author: "jp@x", page, at: "2026-10-17T00:00:00Z",
  points: [{ x: 0.1, y: 0.1 }, { x: 0.5, y: 0.5 }],
});

const station = (marks: Mark[]): WorkspaceState => ({
  id: "w", name: "s", conversation_id: null, page: null, revision: 1, focused_instance: null, visuals: [], marks,
});

/** What the canvas does: the bar's two buttons and the layer, over one stage holding a tile and a record row. */
function Harness({ workspace }: { readonly workspace: WorkspaceState }) {
  const boxes = useBoxes(workspace);
  return (
    <div data-testid="stage">
      <button type="button" onClick={boxes.toggle} aria-pressed={boxes.drawing}>pen</button>
      <button type="button" onClick={boxes.clearAll}>clear</button>
      <div data-visual-instance="inst-1" />
      <div data-row={REC} />
      <MarkLayer boxes={boxes} />
    </div>
  );
}

/** A page's own Escape, as a record page's `detail.back` binds it, mounted before the canvas's pen. */
function Page({ back }: { readonly back: () => void }) {
  useCommands([{ id: "detail.back", title: "Back", keys: ["Escape"], run: back }]);
  return null;
}

function mount(marks: Mark[] = [], back: () => void = () => {}) {
  render(
    <QueryClientProvider client={createQueryClient(() => {})}>
      <CommandRegistryContext value={new CommandRegistry()}>
        <Shortcuts />
        <Page back={back} />
        <Harness workspace={station(marks)} />
      </CommandRegistryContext>
    </QueryClientProvider>,
  );
  const rect = { left: 0, top: 0, width: 1000, height: 500, right: 1000, bottom: 500, x: 0, y: 0, toJSON: () => ({}) };
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue(rect);
}

beforeEach(() => {
  location.hash = "#/graph";
  vi.mocked(drawMark).mockResolvedValue(station([]));
  vi.mocked(removeMark).mockResolvedValue(station([]));
  vi.mocked(clearMarks).mockResolvedValue(station([]));
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
  expect(rule(".mark-svg")).toContain("pointer-events: none");
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
