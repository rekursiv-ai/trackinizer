import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, onTestFinished, test, vi } from "vitest";
import { storageKey } from "../state/store";
import { type BrowserState, EMPTY_STATE, exportState } from "../state/value";
import { BrowserDataSection, importSummary } from "./BrowserData";
import { FAST, profile, renderScreen, stubClipboard } from "./testing";

const ADA = profile("writer");
const VIEW = { id: "v1", name: "Mine", request: { kinds: ["Issue"], filters: [{ field: "account", op: "is" as const, value: "ada@example.com" }] } };

const MINE: BrowserState = {
  ...EMPTY_STATE,
  stars: ["a"],
  views: [VIEW],
  aliases: ["ada"],
  read: { boundary: "2026-09-20T10:00:00+00:00", marks: [] },
  ui: { collapsed: ["browse"], lens: "lineage" },
};

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(storageKey(ADA.email), JSON.stringify(MINE));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function stored(): BrowserState {
  return JSON.parse(localStorage.getItem(storageKey(ADA.email))!) as BrowserState;
}

function file(name: string, text: string): File {
  return new File([text], name, { type: "application/json" });
}

test("Copy as JSON and Download both export the whole stored state", async () => {
  const copied = stubClipboard();
  const saved: { name: string; text: Promise<string> }[] = [];
  let blob: Blob | null = null;
  // jsdom has no object URLs.
  Object.assign(URL, { createObjectURL: (made: Blob) => ((blob = made), "blob:export"), revokeObjectURL: () => {} });
  onTestFinished(() => {
    Reflect.deleteProperty(URL, "createObjectURL");
    Reflect.deleteProperty(URL, "revokeObjectURL");
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    saved.push({ name: this.download, text: blob!.text() });
  });
  renderScreen(<BrowserDataSection />, ADA);
  expect(screen.getByText("Stars").nextSibling?.textContent).toBe("1");
  expect(screen.getByText("Saved views").nextSibling?.textContent).toBe("1");

  fireEvent.click(screen.getByRole("button", { name: "Copy as JSON" }));
  await waitFor(() => expect(copied).toEqual([exportState(MINE)]), FAST);
  fireEvent.click(screen.getByRole("button", { name: "Download JSON" }));
  expect(saved).toHaveLength(1);
  expect(saved[0]!.name).toMatch(/^trackinizer-ada@example\.com-\d{4}-\d{2}-\d{2}\.json$/);
  expect(JSON.parse(await saved[0]!.text)).toEqual(MINE);
});

test("Copy as JSON says it failed when the browser has no clipboard", async () => {
  renderScreen(<BrowserDataSection />, ADA);
  fireEvent.click(screen.getByRole("button", { name: "Copy as JSON" }));
  await screen.findByText("Could not copy: the browser refused the clipboard.");
});

test("an import merges by the rules, keeps this browser's UI state, and says what it added", async () => {
  const theirs: BrowserState = {
    ...EMPTY_STATE,
    stars: ["a", "b"],
    views: [{ ...VIEW, name: "Mine, renamed" }],
    aliases: ["Agent"],
    people: { bob: { name: "Bob", type: "person" } },
    read: { boundary: "2026-09-25T10:00:00+00:00", marks: ["c9"] },
    ui: { collapsed: [], lens: "details" },
  };
  renderScreen(<BrowserDataSection />, ADA);
  await userEvent.upload(screen.getByLabelText("Import JSON file"), file("other.json", exportState(theirs)));
  await screen.findByText("Imported 1 star, 1 saved view, 1 name, 1 person and read state.");
  const merged = stored();
  expect(merged.stars).toEqual(["a", "b"]);
  expect(merged.views.map((view) => view.name)).toEqual(["Mine", "Mine, renamed (imported)"]);
  expect(merged.aliases).toEqual(["ada", "Agent"]);
  expect(merged.read).toEqual({ boundary: "2026-09-25T10:00:00+00:00", marks: ["c9"] });
  expect(merged.ui).toEqual(MINE.ui);

  // The same file again adds nothing.
  await userEvent.upload(screen.getByLabelText("Import JSON file"), file("other.json", exportState(theirs)));
  await screen.findByText("Nothing to import: this browser already has everything in the file.");
  expect(stored()).toEqual(merged);
});

test("a file that is not an export is refused with the reason, and nothing changes", async () => {
  renderScreen(<BrowserDataSection />, ADA);
  await userEvent.upload(screen.getByLabelText("Import JSON file"), file("notes.json", "stars: a"));
  expect((await screen.findByRole("alert")).textContent).toBe("Could not import notes.json. It is not JSON.");
  await userEvent.upload(screen.getByLabelText("Import JSON file"), file("x.json", '{"version": 1, "stars": "a"}'));
  await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("Could not import x.json. stars must be a list."));
  expect(stored()).toEqual(MINE);
});

test("the summary names each kind of addition once, in one sentence", () => {
  expect(importSummary(EMPTY_STATE, { ...EMPTY_STATE, stars: ["a", "b"] })).toBe("Imported 2 stars.");
  expect(importSummary(EMPTY_STATE, { ...EMPTY_STATE, stars: ["a"], aliases: ["x", "y"] })).toBe("Imported 1 star and 2 names.");
  expect(importSummary(EMPTY_STATE, EMPTY_STATE)).toBe("Nothing to import: this browser already has everything in the file.");
});
