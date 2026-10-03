import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { renderDetail, row } from "../detail/testing";
import { stubLayout } from "../editors/testing";
import { cacheWith, serveGraph } from "./testing";

const OLD = row("Issue", 1, { title: "Old plan" });
const NEWER = row("Paper", 2, { title: "A newer paper" });

beforeEach(() => {
  history.replaceState(null, "", "#/ref/Issue/1");
  stubLayout();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function open(server = serveGraph([OLD, NEWER])) {
  renderDetail({ id: OLD.id }, cacheWith([OLD, NEWER]));
  await screen.findByRole("heading", { level: 1 });
  return server;
}

/**
 * Pick `action` from the detail's ⋯ menu, its button found by label and the option
 * by text: a query by role works out the style of the whole detail first, some
 * 10 ms a call.
 */
function more(action: string) {
  fireEvent.click(screen.getByLabelText("Issue#1 actions", { selector: "button" }));
  fireEvent.click([...document.querySelectorAll("[role=option]")].find((option) => option.textContent!.startsWith(action))!);
}

test("the ⋯ menu adds a relation or supersedes both ways, and R opens the picker, which gives focus back", async () => {
  await open();
  const trigger = screen.getByRole("button", { name: "Issue#1 actions" });
  fireEvent.click(trigger);
  expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
    "Add relation…R",
    "Supersede with an existing inquiry…",
    "Supersede with a new inquiry…",
    "Purge…",
  ]);
  fireEvent.keyDown(screen.getByRole("combobox"), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("combobox")).toBeNull());

  const button = screen.getByRole("button", { name: "Add parent" });
  button.focus();
  await userEvent.keyboard("r");
  expect(within(screen.getByRole("dialog", { name: "Add relation" })).getByRole("combobox", { name: "Relation" })).toBeTruthy();
  fireEvent.keyDown(screen.getByRole("combobox", { name: "Relation" }), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(document.activeElement).toBe(button);
});

test("Supersede with an existing inquiry is one supersedes POST from the inquiry picked, of any kind", async () => {
  const server = await open();
  more("Supersede with an existing inquiry");
  const dialog = screen.getByRole("dialog", { name: "Superseded by…" });
  expect(dialog.querySelector(".pal-ctx")!.textContent).toMatch(/^Issue#1Superseded bypick one or more issues, /);
  const search = within(dialog).getByRole("combobox");
  fireEvent.keyDown(search, { key: "Enter" });
  fireEvent.click(within(dialog).getByRole("button", { name: /Add 1 relation/ }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(server.writes()).toEqual([{ call: `POST /api/edges/${NEWER.id}/supersedes/${OLD.id}`, body: {} }]);
  expect(await screen.findByText("Linked: Issue#1 superseded by Paper#2")).toBeTruthy();
  await within(screen.getByRole("region", { name: "Children" })).findByText("superseded_by", { selector: ".edge-name" });
});

test("Supersede with a new inquiry is one batch of the new row and its edge, then opens the new one", async () => {
  const server = await open();
  more("Supersede with a new inquiry");
  const dialog = screen.getByRole("dialog", { name: "Supersede Issue#1 with a new issue" });
  const title = within(dialog).getByRole("textbox", { name: "Title" });
  expect(title).toHaveProperty("value", "Old plan");
  fireEvent.change(title, { target: { value: "New plan" } });
  fireEvent.change(within(dialog).getByRole("textbox", { name: "Description" }), { target: { value: "Replaces the old plan." } });
  fireEvent.keyDown(title, { key: "Enter", metaKey: true });
  await waitFor(() => expect(location.hash).toBe(`#/lookup/${server.rows.at(-1)!.id}`));
  expect(screen.getByText("Created a new issue that supersedes Issue#1")).toBeTruthy();
  const [write] = server.writes();
  const items = (write!.body as { items: { idempotency_key: string }[] }).items;
  expect(server.writes()).toEqual([
    {
      call: "POST /api/inquiries/batch",
      body: {
        items: [{ kind: "Issue", title: "New plan", description: "Replaces the old plan.", idempotency_key: items[0]!.idempotency_key }],
        edges: [{ edge_kind: "supersedes", from_index: 0, to_id: OLD.id }],
      },
    },
  ]);
});

test("a batch the server keeps failing is retried with the same keys, then offers Retry and Discard", async () => {
  const server = serveGraph([OLD, NEWER]);
  const unavailable = () => Response.json({ detail: "database unavailable" }, { status: 503 });
  server.answers.push(unavailable, unavailable, unavailable, unavailable);
  await open(server);
  more("Supersede with a new inquiry");
  const dialog = screen.getByRole("dialog");
  // After rendering: Testing Library's polling waits on the real `setTimeout`.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(within(dialog).getByRole("button", { name: /Create and supersede/ }));
  await act(() => vi.runAllTimersAsync());
  vi.useRealTimers();
  expect(within(dialog).getByText("Not saved: the server failed (database unavailable).")).toBeTruthy();
  expect(server.writes()).toHaveLength(4);
  fireEvent.click(within(dialog).getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(location.hash).toBe(`#/lookup/${server.rows.at(-1)!.id}`));
  // Every send is the same request: the same row key.
  expect(new Set(server.writes().map((write) => JSON.stringify(write.body))).size).toBe(1);
});

test("an AgentSession is not superseded by a new one, which only trax run records (CR-W03)", async () => {
  const session = row("AgentSession", 1, { title: "A session" });
  history.replaceState(null, "", "#/ref/AgentSession/1");
  serveGraph([session]);
  renderDetail({ id: session.id }, cacheWith([session]));
  await screen.findByRole("heading", { level: 1 });
  fireEvent.click(screen.getByRole("button", { name: "AgentSession#1 actions" }));
  const actions = screen.getAllByRole("option").map((option) => option.textContent);
  expect(actions).toContain("Supersede with an existing inquiry…");
  expect(actions).not.toContain("Supersede with a new inquiry…");
});

test("a supersede dialog closed while its batch is sent opens nothing once it lands, and the toast says it landed (RV-04)", async () => {
  const server = serveGraph([OLD, NEWER]);
  await open(server);
  more("Supersede with a new inquiry");
  const dialog = screen.getByRole("dialog");
  const release = server.hold("POST", "/api/inquiries/batch");
  fireEvent.click(within(dialog).getByRole("button", { name: /Create and supersede/ }));
  await within(dialog).findByText("Saving…");
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  release();
  expect(await screen.findByText("Created a new issue that supersedes Issue#1")).toBeTruthy();
  expect(location.hash).toBe("#/ref/Issue/1");
  expect(server.rows).toHaveLength(3);
});
