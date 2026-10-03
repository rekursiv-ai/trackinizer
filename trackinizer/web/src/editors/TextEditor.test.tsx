import { QueryClient } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Session } from "../app/session";
import { detailQueries } from "../detail/queries";
import { renderDetail, row } from "../detail/testing";
import { serveRow, stubLayout } from "./testing";

beforeEach(() => {
  history.replaceState(null, "", "#/ref/Issue/1");
  localStorage.clear();
  stubLayout();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const unavailable = () => Response.json({ detail: "database unavailable" }, { status: 503 });

async function open(server: ReturnType<typeof serveRow>, session?: Session) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = renderDetail({ id: server.self.id }, queryClient, { session });
  await screen.findByRole("heading", { level: 1 });
  return { queryClient, view };
}

const heading = () => screen.getByRole("heading", { level: 1 });
// By its label: a query by role works out the style of the whole detail first,
// some 10 ms a call, and these tests call this one often.
const description = () => screen.getByLabelText("Description", { selector: "textarea" }) as HTMLTextAreaElement;

test("the title edits in place, and Enter saves it on one line", async () => {
  const server = serveRow(row("Issue", 1, { title: "Old title" }));
  await open(server);
  fireEvent.click(within(heading()).getByRole("button"));
  fireEvent.change(screen.getByRole("textbox", { name: "Title" }), { target: { value: "  New\ntitle " } });
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Title" }), { key: "Enter" });
  await waitFor(() => expect(heading().textContent).toBe("New title"));
  expect(server.writes()).toEqual([{ call: "PUT title", body: { value: "New title" } }]);
});

test("Escape puts the title back, and an empty title is not saved", async () => {
  const server = serveRow(row("Issue", 1, { title: "Old title" }));
  await open(server);
  fireEvent.click(within(heading()).getByRole("button"));
  fireEvent.change(screen.getByRole("textbox", { name: "Title" }), { target: { value: "Dropped" } });
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Title" }), { key: "Escape" });
  expect(heading().textContent).toBe("Old title");

  fireEvent.click(within(heading()).getByRole("button"));
  fireEvent.change(screen.getByRole("textbox", { name: "Title" }), { target: { value: " " } });
  fireEvent.blur(screen.getByRole("textbox", { name: "Title" }));
  expect(heading().textContent).toBe("Old title");
  expect(server.writes()).toEqual([]);
});

test("an open description draft survives an unrelated re-render and a live refetch (COLD-02)", async () => {
  const server = serveRow(row("Issue", 1, { description: "First" }));
  const { queryClient } = await open(server);
  fireEvent.click(screen.getByText("First"));
  fireEvent.change(description(), { target: { value: "First, and more" } });

  // Another user edits another field; the stream refetches the detail.
  server.change("priority", 10, "josh@example.com");
  await act(() => queryClient.refetchQueries({ queryKey: detailQueries.detail(server.self.id).queryKey }));
  await screen.findByRole("img", { name: "P1 High" });
  expect(description().value).toBe("First, and more");
  expect(document.querySelector(".ed-changed")).toBeNull();

  fireEvent.keyDown(description(), { key: "Enter", metaKey: true });
  await waitFor(() => expect(screen.queryByRole("textbox", { name: "Description" })).toBeNull());
  expect(screen.getByText("First, and more")).toBeTruthy();
  expect(server.writes()).toEqual([{ call: "PUT description", body: { value: "First, and more" } }]);
});

test("a description changed under the draft says who, shows theirs, and keeps mine on request, with a reason", async () => {
  const server = serveRow(row("Issue", 1, { description: "First" }));
  const { queryClient } = await open(server);
  fireEvent.click(screen.getByRole("button", { name: "Edit Description" }));
  fireEvent.change(description(), { target: { value: "Mine" } });
  server.change("description", "Theirs", "josh@example.com");
  await act(() => queryClient.refetchQueries({ queryKey: detailQueries.detail(server.self.id).queryKey }));

  await waitFor(() => expect(document.querySelector(".ed-changed")).not.toBeNull());
  const notice = document.querySelector<HTMLElement>(".ed-changed")!;
  expect(notice.textContent).toContain("josh@example.com changed description while you were editing.");
  expect(within(notice).getByText("Theirs", { selector: "pre" })).toBeTruthy();
  expect(description().value).toBe("Mine");

  fireEvent.click(within(notice).getByRole("button", { name: "Keep mine" }));
  expect(document.querySelector(".ed-changed")).toBeNull();
  fireEvent.change(screen.getByRole("textbox", { name: "Reason" }), { target: { value: "Mine is newer" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(screen.queryByRole("textbox", { name: "Description" })).toBeNull());
  expect(server.writes()).toEqual([{ call: "PUT description", body: { value: "Mine", reason: "Mine is newer" } }]);
});

const doneWhen = () => screen.getByRole("textbox", { name: "Done when" }) as HTMLTextAreaElement;

test("Use theirs replaces the draft", async () => {
  const server = serveRow(row("Issue", 1, { validation: "Tests pass" }));
  const { queryClient } = await open(server);
  fireEvent.click(screen.getByRole("button", { name: "Edit Done when" }));
  fireEvent.change(doneWhen(), { target: { value: "Mine" } });
  server.change("validation", "CI is green", "josh@example.com");
  await act(() => queryClient.refetchQueries({ queryKey: detailQueries.detail(server.self.id).queryKey }));
  fireEvent.click(await screen.findByRole("button", { name: "Use theirs" }));
  expect(doneWhen().value).toBe("CI is green");
  expect(server.writes()).toEqual([]);
});

test("an emptied field clears with DELETE", async () => {
  const server = serveRow(row("Issue", 1, { validation: "CI is green" }));
  await open(server);
  fireEvent.click(screen.getByRole("button", { name: "Edit Done when" }));
  fireEvent.change(doneWhen(), { target: { value: "  " } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(screen.getByRole("region", { name: "Done when" }).textContent).toBe("Done whenNot set"));
  expect(server.writes()).toEqual([{ call: "DELETE validation", body: {} }]);
});

test("E opens the description; Escape drops the draft without a write", async () => {
  const server = serveRow(row("Issue", 1));
  await open(server);
  await userEvent.keyboard("e");
  fireEvent.change(await screen.findByRole("textbox", { name: "Description" }), { target: { value: "Draft" } });
  fireEvent.keyDown(description(), { key: "Escape" });
  expect(screen.queryByRole("textbox", { name: "Description" })).toBeNull();
  expect(screen.getByText("No description.")).toBeTruthy();
  expect(server.writes()).toEqual([]);
});

test("a draft open when the session ends is kept, and reopens on the next load", async () => {
  const server = serveRow(row("Issue", 1, { description: "First" }));
  const session = new Session(() => {});
  await open(server, session);
  fireEvent.click(screen.getByText("First"));
  fireEvent.change(description(), { target: { value: "Unsaved words" } });
  session.leaveForLogin();
  cleanup();

  await open(server);
  expect(description().value).toBe("Unsaved words");
  expect(localStorage.length).toBe(0);
});

test("text that is not JSON is refused before sending", async () => {
  const server = serveRow(row("Experiment", 1, { config: { lr: 0.1 } }));
  await open(server);
  fireEvent.click(screen.getByRole("button", { name: "Edit Config" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Config" }), { target: { value: "{lr: 0.2}" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(screen.getByRole("alert").textContent).toMatch(/^Not valid JSON/);
  expect(screen.getByRole("textbox", { name: "Config" })).toHaveProperty("value", "{lr: 0.2}");
  expect(server.writes()).toEqual([]);
});

test("a config edits as JSON", async () => {
  const server = serveRow(row("Experiment", 1, { config: { lr: 0.1 } }));
  await open(server);
  fireEvent.click(screen.getByRole("button", { name: "Edit Config" }));
  const config = screen.getByRole("textbox", { name: "Config" }) as HTMLTextAreaElement;
  expect(JSON.parse(config.value)).toEqual({ lr: 0.1 });
  fireEvent.change(config, { target: { value: '{"lr": 0.2}' } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(screen.queryByRole("textbox", { name: "Config" })).toBeNull());
  expect(server.writes()).toEqual([{ call: "PUT config", body: { value: { lr: 0.2 } } }]);
});

test("while a save checks and sends, the editor can be neither changed nor cancelled; it closes once saved (R9-08)", { tags: ["manual"] }, async () => {
  const server = serveRow(row("Issue", 1, { description: "First" }));
  await open(server);
  fireEvent.click(screen.getByRole("button", { name: "Edit Description" }));
  fireEvent.change(description(), { target: { value: "Second" } });
  const release = server.hold("GET", `/api/inquiries/${server.self.id}`);
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Saving…");
  expect(screen.getByRole("button", { name: "Cancel" }).hasAttribute("disabled")).toBe(true);
  fireEvent.keyDown(description(), { key: "Escape" });
  expect(description().readOnly).toBe(true);
  expect((screen.getByRole("textbox", { name: "Reason" }) as HTMLInputElement).readOnly).toBe(true);
  release();
  await waitFor(() => expect(screen.queryByRole("textbox", { name: "Description" })).toBeNull());
  expect(server.writes()).toEqual([{ call: "PUT description", body: { value: "Second" } }]);
});

test("a title being saved takes no more typing, so its save cannot drop it (CR-W04)", async () => {
  const server = serveRow(row("Issue", 1, { title: "Old title" }));
  await open(server);
  fireEvent.click(within(heading()).getByRole("button"));
  const title = screen.getByRole("textbox", { name: "Title" }) as HTMLTextAreaElement;
  fireEvent.change(title, { target: { value: "New title" } });
  const release = server.hold("PUT", `/api/inquiries/${server.self.id}/title`);
  fireEvent.keyDown(title, { key: "Enter" });
  await screen.findByText("Saving…");
  expect(title.readOnly).toBe(true);
  fireEvent.keyDown(title, { key: "Escape" });
  expect(screen.getByRole("textbox", { name: "Title" })).toBe(title);
  release();
  await waitFor(() => expect(heading().textContent).toBe("New title"));
});

// Retry sends the text the save failed with, so the editor must still show
// exactly that text while Retry is offered; Discard hands the draft back.
test("after a failed save the description is locked until Retry or Discard; Discard keeps the draft to save again", { tags: ["manual"] }, async () => {
  const server = serveRow(row("Issue", 1, { description: "First" }));
  await open(server);
  fireEvent.click(screen.getByRole("button", { name: "Edit Description" }));
  fireEvent.change(description(), { target: { value: "Second" } });
  server.answers.push(unavailable, unavailable, unavailable, unavailable);
  // After rendering: Testing Library's polling waits on the real `setTimeout`.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await act(() => vi.runAllTimersAsync());

  expect(screen.getByRole("alert").textContent).toContain("the server failed");
  expect(description().readOnly).toBe(true);
  expect((screen.getByRole("textbox", { name: "Reason" }) as HTMLInputElement).readOnly).toBe(true);
  expect(screen.getByRole("button", { name: "Save" }).hasAttribute("disabled")).toBe(true);

  // Discard settles the failed write a microtask later; flush it, as any real
  // click after it would come later still.
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Discard" })));
  expect(description().readOnly).toBe(false);
  expect(description().value).toBe("Second");
  fireEvent.change(description(), { target: { value: "Third" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await act(() => vi.runAllTimersAsync());
  expect(screen.queryByRole("textbox", { name: "Description" })).toBeNull();
  expect(server.writes().at(-1)).toEqual({ call: "PUT description", body: { value: "Third" } });
});

test("after a failed save the title is locked until Retry or Discard", async () => {
  const server = serveRow(row("Issue", 1, { title: "Old title" }));
  await open(server);
  fireEvent.click(within(heading()).getByRole("button"));
  const title = screen.getByRole("textbox", { name: "Title" }) as HTMLTextAreaElement;
  fireEvent.change(title, { target: { value: "New title" } });
  server.answers.push(unavailable, unavailable, unavailable, unavailable);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.keyDown(title, { key: "Enter" });
  await act(() => vi.runAllTimersAsync());

  expect(title.readOnly).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  expect(title.readOnly).toBe(false);
  expect(title.value).toBe("New title");
});

// One editor per test: opening the description takes focus from an open title,
// and that blur saves the title, a write that outlives the test.
test("Enter or Escape pressed while an input method composes neither saves nor closes the title (BR-08)", async () => {
  const server = serveRow(row("Issue", 1, { title: "Old title" }));
  await open(server);
  fireEvent.click(within(heading()).getByRole("button"));
  const title = screen.getByRole("textbox", { name: "Title" });
  fireEvent.change(title, { target: { value: "日本" } });
  fireEvent.keyDown(title, { key: "Enter", isComposing: true });
  fireEvent.keyDown(title, { key: "Escape", isComposing: true });
  expect(screen.getByRole("textbox", { name: "Title" })).toHaveProperty("value", "日本");
  expect(server.writes()).toEqual([]);
});

test("⌘↵ or Escape pressed while an input method composes neither saves nor closes the description (BR-08)", async () => {
  const server = serveRow(row("Issue", 1, { description: "First" }));
  await open(server);
  fireEvent.click(screen.getByRole("button", { name: "Edit Description" }));
  fireEvent.change(description(), { target: { value: "語" } });
  fireEvent.keyDown(description(), { key: "Enter", metaKey: true, isComposing: true });
  fireEvent.keyDown(description(), { key: "Escape", isComposing: true });
  expect(description().value).toBe("語");
  expect(server.writes()).toEqual([]);
});
