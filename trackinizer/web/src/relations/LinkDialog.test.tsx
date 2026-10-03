import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { renderDetail, row } from "../detail/testing";
import { stubLayout } from "../editors/testing";
import { SEARCH_DEBOUNCE_MS } from "../palette/sources";
import { cacheWith, serveGraph } from "./testing";

const SELF = row("Issue", 1);
const ALPHA = row("Issue", 2, { title: "Alpha plan" });
const BETA = row("Issue", 3, { title: "Beta plan" });
const CLAIM = row("Belief", 4, { title: "Alpha claim" });
const PARENT = row("Issue", 5, { title: "Already the parent" });
const ROWS = [SELF, ALPHA, BETA, CLAIM, PARENT];

beforeEach(() => {
  history.replaceState(null, "", "#/ref/Issue/1");
  stubLayout();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** Serve the graph where Issue#1 narrows Issue#5, and open Issue#1 over a cache that has loaded every row. */
async function open(server = serveGraph(ROWS, [{ from: SELF.id, kind: "narrows", to: PARENT.id }])) {
  renderDetail({ id: SELF.id }, cacheWith(ROWS));
  await screen.findByRole("heading", { level: 1 });
  fireEvent.click(screen.getByRole("button", { name: "Add parent" }));
  return server;
}

/**
 * The open step's search box, found by selector: a query by role works out the
 * style of the whole detail behind the dialog, some 10 ms on each key pressed.
 */
const box = () => document.querySelector<HTMLElement>('[role="dialog"] input[role="combobox"]')!;

/** Each row's text, by its section's heading. */
function listed(): { [section: string]: (string | null)[] } {
  const groups = within(screen.getByRole("listbox")).queryAllByRole("group");
  return Object.fromEntries(
    groups.map((group) => [group.querySelector(".pal-sec")!.textContent, within(group).getAllByRole("option").map((option) => option.textContent)]),
  );
}

function key(key: string, modifiers: { metaKey?: boolean } = {}) {
  fireEvent.keyDown(box(), { key, ...modifiers });
}

test("the picker offers only the relations the topology gives this kind, then only admitted, unrelated inquiries", async () => {
  await open();
  expect(listed()).toEqual({
    Relation: [
      "Narrows…issues",
      "Narrowed by…issues",
      "Requires…issues",
      "Required by…issues",
      "Produced by…any inquiry",
      "Produces…any inquiry",
      "Supersedes…any inquiry",
      "Superseded by…any inquiry",
    ],
  });
  key("Enter");
  expect(screen.getByRole("dialog").querySelector(".pal-ctx")!.textContent).toBe("Issue#1Narrowspick one or more issues");
  // Not itself, not the Belief, not the Issue it already narrows.
  expect(listed()).toEqual({ Inquiries: ["Issue#2Alpha plan", "Issue#3Beta plan"] });
  // Backspace in the empty box goes back a step.
  key("Backspace");
  expect(box().getAttribute("placeholder")).toBe("Relation type…");
});

test("ticked inquiries are added one POST each, child to parent, then the picker closes and says so", { tags: ["manual"] }, async () => {
  const server = await open();
  fireEvent.change(box(), { target: { value: "narrowed" } });
  key("Enter");
  key("Enter");
  key("ArrowDown");
  key("Enter");
  // Issue#5 is a parent here, not a child: the server, not the picker, refuses a cycle.
  expect(screen.getAllByRole("option").map((option) => [option.textContent, option.getAttribute("aria-selected")])).toEqual([
    ["Issue#2Alpha plan", "true"],
    ["Issue#3Beta plan", "true"],
    ["Issue#5Already the parent", "false"],
  ]);
  key("Enter", { metaKey: true });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(server.writes()).toEqual([
    { call: `POST /api/edges/${ALPHA.id}/narrows/${SELF.id}`, body: {} },
    { call: `POST /api/edges/${BETA.id}/narrows/${SELF.id}`, body: {} },
  ]);
  const keys = server.sent.filter((request) => request.method === "POST").map((request) => request.headers["idempotency-key"]);
  expect(new Set(keys).size).toBe(2);
  expect(await screen.findByText("Linked 2 inquiries: Issue#1 narrowed by each")).toBeTruthy();
  const children = screen.getByRole("region", { name: "Children" });
  await waitFor(() =>
    expect(
      [...children.querySelectorAll(".rail-peer")].map((peer) => [
        peer.querySelector(".rail-link")!.textContent,
        peer.querySelector(".edge-name")!.textContent,
      ]),
    ).toEqual([
      ["Alpha plan Issue#2", "narrowed_by"],
      ["Beta plan Issue#3", "narrowed_by"],
    ]),
  );
});

test("a failed add stays ticked and marked with the server's message; those added before it leave the picks", { tags: ["manual"] }, async () => {
  const server = serveGraph(ROWS);
  server.answers.push(
    () => null,
    () => Response.json({ detail: "adding this edge would close a cycle", code: "conflict" }, { status: 409 }),
  );
  await open(server);
  key("Enter");
  key("Enter");
  key("ArrowDown");
  key("Enter");
  fireEvent.click(screen.getByRole("button", { name: /Add 2 relations/ }));
  expect(await screen.findByText("adding this edge would close a cycle")).toBeTruthy();
  expect(screen.getByText("Linked: Issue#1 narrows Issue#2")).toBeTruthy();
  const failed = screen.getByRole("option", { selected: true });
  expect(failed.textContent).toBe("Issue#3Beta planNot added");
  expect(screen.getByRole("button", { name: /Add 1 relation/ })).toBeTruthy();
  expect(server.writes().map((write) => write.call)).toEqual([
    `POST /api/edges/${SELF.id}/narrows/${ALPHA.id}`,
    `POST /api/edges/${SELF.id}/narrows/${BETA.id}`,
  ]);
});

test("a Kind#seq not loaded is looked up, and the server is searched after a pause for admitted kinds only", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const far = row("Issue", 7, { title: "Gamma far" });
  const server = serveGraph([SELF, far, row("Belief", 8, { title: "Gamma claim" })]);
  renderDetail({ id: SELF.id }, cacheWith([SELF]));
  await pass();
  fireEvent.click(screen.getByRole("button", { name: "Add parent" }));
  fireEvent.change(box(), { target: { value: "requires" } });
  key("Enter");

  fireEvent.change(box(), { target: { value: "Issue#7" } });
  // Its id, then its detail, for the title the row shows. Under fake timers,
  // vi.waitFor waits its interval for real between checks: 50 ms by default,
  // where each read needs only a turn of the event loop.
  await vi.waitFor(() => expect(screen.queryByRole("option", { name: /Gamma far/ })).not.toBeNull(), { interval: 1 });
  expect(listed()).toEqual({ "Jump to": ["Issue#7Gamma far"] });

  fireEvent.change(box(), { target: { value: "gamma" } });
  await pass(SEARCH_DEBOUNCE_MS);
  // Its detail, read for the jump, is now loaded, so it lists among loaded inquiries.
  expect(listed()).toEqual({ Inquiries: ["Issue#7Gamma far"] });
  const searches = server.sent.filter((request) => request.path === "/api/web/search");
  expect(searches.map((request) => request.query)).toEqual(["?q=gamma&kind=Issue&limit=5"]);

  key("Enter");
  key("Enter", { metaKey: true });
  await pass();
  expect(server.writes()).toEqual([{ call: `POST /api/edges/${SELF.id}/requires/${far.id}`, body: {} }]);
});

/** Let `ms` pass, then what it set off settle: fetches answer in promises, the cache on timers. */
async function pass(ms = 0) {
  await act(() => vi.advanceTimersByTimeAsync(ms));
  await act(() => vi.advanceTimersByTimeAsync(0));
  await act(() => vi.advanceTimersByTimeAsync(0));
}

test("while the picker adds, its rows take no ticks, so none is dropped when it closes (CR-W04)", async () => {
  const server = serveGraph(ROWS, [{ from: SELF.id, kind: "narrows", to: PARENT.id }]);
  await open(server);
  key("Enter");
  key("Enter");
  const release = server.hold("POST", `/api/edges/${SELF.id}/narrows/${ALPHA.id}`);
  key("Enter", { metaKey: true });
  await within(screen.getByRole("dialog")).findByText("Saving…");
  key("ArrowDown");
  key("Enter");
  fireEvent.click(screen.getByRole("option", { name: /Beta plan/ }));
  expect(screen.getByRole("option", { name: /Beta plan/ }).getAttribute("aria-selected")).toBe("false");
  expect(screen.getByRole("option", { name: /Beta plan/ }).getAttribute("aria-disabled")).toBe("true");
  release();
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(server.writes()).toEqual([{ call: `POST /api/edges/${SELF.id}/narrows/${ALPHA.id}`, body: {} }]);
});

test("a UUID the app has not loaded is looked up, and can be picked (E8-04)", async () => {
  const far = row("Issue", 7, { title: "Gamma far" });
  const server = serveGraph([SELF, far]);
  renderDetail({ id: SELF.id }, cacheWith([SELF]));
  await screen.findByRole("heading", { level: 1 });
  fireEvent.click(screen.getByRole("button", { name: "Add parent" }));
  fireEvent.change(box(), { target: { value: "requires" } });
  key("Enter");
  fireEvent.change(box(), { target: { value: far.id.toUpperCase() } });
  await waitFor(() => expect(listed()).toEqual({ "Jump to": ["Issue#7Gamma far"] }));
  key("Enter");
  key("Enter", { metaKey: true });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(server.writes()).toEqual([{ call: `POST /api/edges/${SELF.id}/requires/${far.id}`, body: {} }]);
});
