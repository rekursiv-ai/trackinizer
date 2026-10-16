import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { caretPoint } from "./caretPoint";
import { mentionAt } from "../console/mentions";
import { type Completion, splice, type Target, useCompletion } from "./CompletionField";
import type { FieldProps } from "./Composer";

vi.mock("./caretPoint", async (real) => ({ ...(await real<typeof import("./caretPoint")>()), caretPoint: vi.fn() }));

beforeEach(() => {
  // jsdom has no ResizeObserver; the list follows the window's resize here.
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.mocked(caretPoint).mockReturnValue({ left: 0, top: 0, height: 16 });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** A `#` word at the caret: from its `#` to the next space. */
function hashAt(value: string, caret: number): Target | null {
  const start = value.lastIndexOf("#", caret - 1);
  if (start < 0 || /\s/.test(value.slice(start, caret))) return null;
  const space = value.slice(start).search(/\s/);
  return { start, end: space < 0 ? value.length : start + space, query: value.slice(start + 1, caret) };
}

const WORDS = ["apple", "apricot"];

function words(target: Target | null): Completion[] {
  return target
    ? WORDS.filter((word) => word.startsWith(target.query)).map((word) => ({ key: word, title: word, label: word, insert: `#${word}` }))
    : [];
}

/** A box completing `#` words, whose own Enter calls `sent`. */
function show() {
  const sent = vi.fn();
  function Host() {
    const [value, setValue] = useState("");
    const field: FieldProps = {
      id: "box",
      value,
      placeholder: "",
      disabled: false,
      rows: 3,
      onChange: (event) => setValue(event.currentTarget.value),
      onKeyDown: (event) => {
        if (!event.defaultPrevented && event.key === "Enter") sent(event.altKey ? "alt" : "plain");
      },
    };
    const { target, render } = useCompletion({ field, edit: setValue, find: hashAt });
    return render(words(target), "Words");
  }
  render(<Host />);
  const box = screen.getByRole<HTMLTextAreaElement>("textbox");
  box.focus();

  return { sent, box, type: (text: string) => fireEvent.change(box, { target: { value: text } }) };
}

function offered(): string[] {
  return screen.queryAllByRole("option").map((option) => option.title);
}

test("modified Enter and Tab never pick; Alt+Enter goes to the box's own handler", () => {
  const { sent, box, type } = show();
  type("#ap");
  expect(offered()).toEqual(["apple", "apricot"]);
  for (const modifier of ["altKey", "ctrlKey", "metaKey", "shiftKey"]) {
    fireEvent.keyDown(box, { key: "Enter", [modifier]: true });
    fireEvent.keyDown(box, { key: "Tab", [modifier]: true });
  }
  expect(box.value).toBe("#ap");
  expect(sent).toHaveBeenCalledWith("alt");
  expect(offered()).toEqual(["apple", "apricot"]);
});

test("Enter picks, keeping the text after the word and putting the caret after the added space", () => {
  const { sent, box, type } = show();
  type("#ap tail");
  box.setSelectionRange(3, 3);
  fireEvent.keyUp(box, { key: "ArrowLeft" });
  fireEvent.keyDown(box, { key: "ArrowDown" });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(box.value).toBe("#apricot tail");
  expect([box.selectionStart, box.selectionEnd]).toEqual([9, 9]);
  expect(sent).not.toHaveBeenCalled();
});

test("modified arrows remain native selection and caret movements", () => {
  const { box, type } = show();
  type("#ap");
  for (const modifier of ["shiftKey", "altKey", "ctrlKey", "metaKey"]) {
    expect(fireEvent.keyDown(box, { key: "ArrowDown", [modifier]: true })).toBe(true);
    expect(screen.getByRole("option", { selected: true }).title).toBe("apple");
  }
});

test("while an input method composes, keys are its own", () => {
  const { box, type } = show();
  type("#ap");
  fireEvent.keyDown(box, { key: "ArrowDown", isComposing: true });
  fireEvent.keyDown(box, { key: "Enter", isComposing: true });
  expect(box.value).toBe("#ap");
  expect(screen.getByRole("option", { selected: true }).title).toBe("apple");
});

test.each<[string, string, number, string, { text: string; caret: number }]>([
  ["at the end of the line, with a space to type the message after", "@ti", 3, "@tiles-a", { text: "@tiles-a ", caret: 9 }],
  ["before the message, the caret past its space", "@ti hello", 3, "@tiles-a", { text: "@tiles-a hello", caret: 9 }],
  ["the whole word, from the caret in its middle", "@tixx hello", 3, "@tiles-a", { text: "@tiles-a hello", caret: 9 }],
  ["a list item, the caret before its comma", "@a,@b hi", 2, "@alpha", { text: "@alpha,@b hi", caret: 6 }],
  ["the last of a list", "@a,@b", 5, "@beta:ops", { text: "@a,@beta:ops ", caret: 13 }],
  ["every agent shown", "@", 1, "@*", { text: "@* ", caret: 3 }],
  ["after leading space", "  @t", 4, "@tiles-a", { text: "  @tiles-a ", caret: 11 }],
])("splice: %s", (_, text, caret, insert, spliced) => {
  expect(splice(text, mentionAt(text, caret)!, insert)).toEqual(spliced);
});

test("the list opens just above the caret's line, from the caret, and follows it as the box scrolls or the window resizes", () => {
  // jsdom lays nothing out: the box and its holder sit at 0, 0 with no size, in a 1000px viewport.
  vi.spyOn(document.documentElement, "clientWidth", "get").mockReturnValue(1000);
  const point = vi.mocked(caretPoint).mockReturnValue({ left: 50, top: 32, height: 16 });
  const { box, type } = show();
  type("one\ntwo #ap");
  const list = screen.getByRole("listbox");
  // Its bottom 4px above the caret line's top, measured up from the holder's bottom.
  expect([list.style.left, list.style.bottom]).toEqual(["50px", "-28px"]);
  expect(point).toHaveBeenLastCalledWith(box, 11);
  point.mockReturnValue({ left: 60, top: 12, height: 16 });
  fireEvent.scroll(box);
  expect([list.style.left, list.style.bottom]).toEqual(["60px", "-8px"]);
  point.mockReturnValue({ left: 990, top: 12, height: 16 });
  vi.spyOn(list, "offsetWidth", "get").mockReturnValue(320);
  act(() => window.dispatchEvent(new Event("resize")));
  expect(list.style.left).toBe("680px");
});

test("the empty hint opens at the caret too", () => {
  vi.spyOn(document.documentElement, "clientWidth", "get").mockReturnValue(1000);
  vi.mocked(caretPoint).mockReturnValue({ left: 24, top: 0, height: 16 });
  function Host() {
    const [value, setValue] = useState("");
    const field: FieldProps = { id: "box", value, placeholder: "", disabled: false, rows: 3, onChange: (event) => setValue(event.currentTarget.value), onKeyDown: () => {} };
    const { render: draw } = useCompletion({ field, edit: setValue, find: hashAt });
    return draw([], "Words", "No words");
  }
  render(<Host />);
  const box = screen.getByRole<HTMLTextAreaElement>("textbox");
  box.focus();
  fireEvent.change(box, { target: { value: "#zz" } });
  expect(screen.getByRole("status").style.left).toBe("24px");
});

test("a pick closes the list on the picked word whatever its sigil's length", () => {
  // `::` words: a sigil of two characters, which `insert.slice(1)` would misread as `:apple`.
  const colons = (value: string, caret: number): Target | null => {
    const start = value.lastIndexOf("::", caret - 2);
    if (start < 0 || /\s/.test(value.slice(start, caret))) return null;
    const space = value.slice(start).search(/\s/);
    return { start, end: space < 0 ? value.length : start + space, query: value.slice(start + 2, caret) };
  };
  function Host() {
    const [value, setValue] = useState("");
    const field: FieldProps = { id: "box", value, placeholder: "", disabled: false, rows: 3, onChange: (event) => setValue(event.currentTarget.value), onKeyDown: () => {} };
    const { target, render: draw } = useCompletion({ field, edit: setValue, find: colons });
    return draw(target ? [{ key: "x", title: "::apple", label: "x", insert: "::apple" }] : [], "Words");
  }
  render(<Host />);
  const box = screen.getByRole<HTMLTextAreaElement>("textbox");
  box.focus();
  fireEvent.change(box, { target: { value: "::a" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(box.value).toBe("::apple ");
  box.setSelectionRange(7, 7);
  fireEvent.keyUp(box, { key: "ArrowLeft" });
  expect(offered()).toEqual([]);
});

test("the target useCompletion hands back is the one find made, with what find put on it", () => {
  type Tagged = Target & { readonly tag: string };
  const seen: (Tagged | null)[] = [];
  function Host() {
    const [value, setValue] = useState("");
    const field: FieldProps = { id: "box", value, placeholder: "", disabled: false, rows: 3, onChange: (event) => setValue(event.currentTarget.value), onKeyDown: () => {} };
    const { target, render: draw } = useCompletion<Tagged>({ field, edit: setValue, find: (text, at) => { const found = hashAt(text, at); return found && { ...found, tag: "mine" }; } });
    seen.push(target);
    return draw([], "Words");
  }
  render(<Host />);
  const box = screen.getByRole<HTMLTextAreaElement>("textbox");
  box.focus();
  fireEvent.change(box, { target: { value: "#ap" } });
  expect(seen.at(-1)).toMatchObject({ start: 0, query: "ap", tag: "mine" });
});

test("the list's observers are set up once while it shows, not again on every render, and go when it closes", () => {
  const observed: string[] = [];
  vi.stubGlobal("ResizeObserver", class { observe() { observed.push("observe"); } unobserve() {} disconnect() { observed.push("disconnect"); } });
  const added = vi.spyOn(window, "addEventListener");
  const { box, type } = show();
  type("#a");
  type("#ap");
  fireEvent.keyDown(box, { key: "ArrowDown" });
  expect(observed).toEqual(["observe"]);
  expect(added.mock.calls.filter(([name]) => name === "resize")).toHaveLength(1);
  type("");
  expect(observed).toEqual(["observe", "disconnect"]);
});
