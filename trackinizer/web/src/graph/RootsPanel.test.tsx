import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { type Command, CommandRegistry, CommandRegistryContext, Shortcuts, useCommands } from "../commands/registry";
import { findRoots, UNROOTED } from "./roots";
import { RootsPanel } from "./RootsPanel";
import { edge, node, uuid } from "./testing";

const threeHoursAgo = new Date(Date.now() - 3 * 3_600_000).toISOString();
/** Noon UTC on 20 September, `minute` past: the same day in any time zone the tests run in. */
const sep20 = (minute: number) => `2026-09-20T12:${String(minute).padStart(2, "0")}:00+00:00`;

/** Root 1 "Alpha" over two Issues; root 2 "Beta", made newer by its Experiment; a Paper under no root. */
const ROOTS = findRoots({
  nodes: [
    node(1, { title: "Alpha", created: sep20(1) }),
    node(11, { created: sep20(11) }),
    node(12, { created: sep20(12) }),
    node(2, { title: "Beta", created: sep20(2) }),
    node(21, { kind: "Experiment", created: threeHoursAgo }),
    node(30, { kind: "Paper", created: sep20(30) }),
  ],
  edges: [edge(11, 1), edge(12, 1), edge(21, 2, "produced_by")],
});

/** Each row as `<ref> <title> <size> <age>`, the marked one starred. */
function rows(): string[] {
  return screen.getAllByRole("option").map((row) => {
    const text = [".roots-ref", ".roots-title", ".roots-size", ".roots-age"].map((part) => row.querySelector(part)!.textContent);
    return `${row.getAttribute("aria-selected") === "true" ? "*" : ""}${text.join(" ").trim()}`;
  });
}

function show(onSelect = vi.fn()) {
  render(<RootsPanel roots={ROOTS} onSelect={onSelect} />);
  return onSelect;
}

beforeEach(() => {
  Element.prototype.scrollIntoView = () => {};
});

afterEach(() => {
  cleanup();
});

test("a row shows the root's kind glyph, Kind#seq, title, subgraph size and newest age; the nodes under no root come last", () => {
  show();
  expect(screen.getByText("2 roots")).toBeTruthy();
  expect(rows()).toEqual(["Issue#2 Beta 2 3h", "Issue#1 Alpha 3 Sep 20", "Unrooted 1 Sep 20"]);
  for (const row of screen.getAllByRole("option")) expect(row.querySelector("svg.ic")).not.toBeNull();
  expect(screen.getAllByRole("option")[0]!.querySelector(".roots-size")!.getAttribute("title")).toBe("1 Issue, 1 Experiment");
});

test("Size sorts the roots biggest first, and Recent back by their newest node; Unrooted stays last", () => {
  show();
  fireEvent.click(screen.getByRole("button", { name: "Size" }));
  expect(screen.getByRole("button", { name: "Size" }).getAttribute("aria-pressed")).toBe("true");
  expect(rows()).toEqual(["Issue#1 Alpha 3 Sep 20", "Issue#2 Beta 2 3h", "Unrooted 1 Sep 20"]);
  fireEvent.click(screen.getByRole("button", { name: "Recent" }));
  expect(rows()[0]).toBe("Issue#2 Beta 2 3h");
});

test("j and k, or the arrows, move through the roots; Enter selects the marked one, and a click any", async () => {
  const onSelect = show();
  const user = userEvent.setup();
  screen.getByRole("listbox").focus();
  await user.keyboard("jj");
  expect(rows()).toEqual(["Issue#2 Beta 2 3h", "*Issue#1 Alpha 3 Sep 20", "Unrooted 1 Sep 20"]);
  await user.keyboard("{ArrowDown}{ArrowDown}");
  expect(rows()[2]).toBe("*Unrooted 1 Sep 20");
  await user.keyboard("k{ArrowUp}");
  expect(rows()[0]).toBe("*Issue#2 Beta 2 3h");
  await user.keyboard("{Enter}");
  expect(onSelect).toHaveBeenLastCalledWith(uuid(2));
  await user.click(screen.getByText("Unrooted"));
  expect(onSelect).toHaveBeenLastCalledWith(UNROOTED);
});

test("typing filters by title or Kind#seq; Enter picks the first match, and Escape clears the filter", async () => {
  const onSelect = show();
  const user = userEvent.setup();
  const filter = screen.getByRole("searchbox", { name: "Filter roots" });
  await user.type(filter, "alp");
  expect(rows()).toEqual(["Issue#1 Alpha 3 Sep 20"]);
  await user.keyboard("{Enter}");
  expect(onSelect).toHaveBeenLastCalledWith(uuid(1));
  await user.clear(filter);
  await user.type(filter, "#2");
  expect(rows()).toEqual(["Issue#2 Beta 2 3h"]);
  await user.type(filter, "x");
  expect(screen.queryAllByRole("option")).toEqual([]);
  expect(screen.getByText("No matches")).toBeTruthy();
  await user.keyboard("{Escape}");
  expect(filter).toHaveProperty("value", "");
  expect(rows()).toHaveLength(3);
});

test("a letter typed on the list goes to the filter", async () => {
  show();
  const user = userEvent.setup();
  screen.getByRole("listbox").focus();
  await user.keyboard("b");
  const filter = screen.getByRole("searchbox", { name: "Filter roots" });
  expect(document.activeElement).toBe(filter);
  expect(filter).toHaveProperty("value", "b");
  expect(rows()).toEqual(["Issue#2 Beta 2 3h"]);
});

test("compact, beside an open Peek, it keeps only each root's glyph, named by its root, and its keys; the box and the sort go", async () => {
  const onSelect = vi.fn();
  render(<RootsPanel roots={ROOTS} onSelect={onSelect} compact />);
  const user = userEvent.setup();
  expect(screen.queryByRole("searchbox", { name: "Filter roots" })).toBeNull();
  expect(screen.queryByRole("group", { name: "Sort roots" })).toBeNull();
  expect(screen.getAllByRole("option").map((row) => [row.getAttribute("aria-label"), row.textContent])).toEqual([
    ["Issue#2 Beta", ""],
    ["Issue#1 Alpha", ""],
    ["Unrooted", ""],
  ]);
  screen.getByRole("listbox").focus();
  await user.keyboard("jj{Enter}");
  expect(onSelect).toHaveBeenLastCalledWith(uuid(1));
  // With no box to take it, a letter filters nothing.
  await user.keyboard("b");
  expect(screen.getAllByRole("option")).toHaveLength(3);
});

test("Enter on Size or Recent sorts, and selects no root", async () => {
  const onSelect = show();
  const user = userEvent.setup();
  screen.getByRole("button", { name: "Size" }).focus();
  await user.keyboard("{Enter}");
  expect(screen.getByRole("button", { name: "Size" }).getAttribute("aria-pressed")).toBe("true");
  expect(rows()[0]).toBe("Issue#1 Alpha 3 Sep 20");
  expect(onSelect).not.toHaveBeenCalled();
});

/** The panel as the graph shows it: compact once a root is selected, as Peek opens on it. */
function Beside({ onSelect }: { onSelect: (key: string) => void }) {
  const [selected, setSelected] = useState(false);
  const select = (key: string) => {
    setSelected(true);
    onSelect(key);
  };
  return <RootsPanel roots={ROOTS} onSelect={select} compact={selected} />;
}

test("a root picked from the filter hands the keys to the list and clears the filter, so the strip beside Peek holds every root", async () => {
  const onSelect = vi.fn();
  render(<Beside onSelect={onSelect} />);
  const user = userEvent.setup();
  await user.type(screen.getByRole("searchbox", { name: "Filter roots" }), "alp{Enter}");
  expect(onSelect).toHaveBeenLastCalledWith(uuid(1));
  expect(document.activeElement).toBe(screen.getByRole("listbox"));
  expect(screen.getAllByRole("option").map((row) => `${row.getAttribute("aria-selected") === "true" ? "*" : ""}${row.getAttribute("aria-label")}`)).toEqual([
    "Issue#2 Beta",
    "*Issue#1 Alpha",
    "Unrooted",
  ]);
  await user.keyboard("j{Enter}");
  expect(onSelect).toHaveBeenLastCalledWith(UNROOTED);
});

function Mount({ commands }: { commands: readonly Command[] }) {
  useCommands(commands);
  return null;
}

test("keys the list has no use for reach the view's shortcuts: . focuses, Esc clears and / searches beside Peek", async () => {
  const ran: string[] = [];
  const command = (key: string): Command => ({ id: key, title: key, keys: [key], run: () => ran.push(key) });
  const onSelect = vi.fn();
  render(
    <CommandRegistryContext value={new CommandRegistry()}>
      <Shortcuts />
      <Mount commands={[".", "Escape", "/", "j"].map(command)} />
      <RootsPanel roots={ROOTS} onSelect={onSelect} compact />
    </CommandRegistryContext>,
  );
  const user = userEvent.setup();
  screen.getByRole("listbox").focus();
  await user.keyboard("j.{Escape}/");
  expect(ran).toEqual([".", "Escape", "/"]);
  // j stays the list's.
  expect(screen.getAllByRole("option")[0]!.getAttribute("aria-selected")).toBe("true");
});
