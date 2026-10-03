import { QueryClient } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Session } from "../app/session";
import { PROFILE, renderDetail, row } from "../detail/testing";
import { prop, propButton, serveRow, stubLayout } from "./testing";

/** My email as a menu option names it, with its "me" hint. */
const ME = new RegExp(`^${PROFILE.email.replaceAll(".", "\\.")}`);

beforeEach(() => {
  history.replaceState(null, "", "#/ref/Issue/1");
  stubLayout();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** Render the served inquiry's detail over `queryClient`, once it has loaded. */
async function open(
  server: ReturnType<typeof serveRow>,
  options?: Parameters<typeof renderDetail>[2],
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  renderDetail({ id: server.self.id }, queryClient, options);
  await screen.findByRole("heading", { level: 1 });
}

/** Open the menu of the property labelled `label` and pick each option in turn. */
async function pick(label: string, ...options: (string | RegExp)[]) {
  fireEvent.click(propButton(label));
  for (const option of options) fireEvent.click(await screen.findByRole("option", { name: option }));
}

/**
 * Close the open menu with Escape, once Radix has handed focus back to its
 * trigger: a menu opened before that would lose focus to it, and close.
 */
async function closeMenu(label: string) {
  fireEvent.keyDown(screen.getByRole("combobox"), { key: "Escape" });
  // Radix hands focus back in a zero-delay timer; this one runs after it.
  await act(() => new Promise((resolve) => setTimeout(resolve)));
  expect(document.activeElement).toBe(propButton(label));
}

test("a status pick writes by compare-and-set", async () => {
  const server = serveRow(row("Issue", 1));
  await open(server);
  await pick("Status", "Complete");
  await waitFor(() => expect(prop("Status").textContent).toBe("Complete"));
  expect(server.writes()).toEqual([{ call: "PUT status", body: { value: "complete", mode: "cas", expected: "active" } }]);
});

test("abandoning asks for a reason first", async () => {
  const server = serveRow(row("Issue", 1));
  await open(server);
  await pick("Status", "Abandoned");
  const dialog = await screen.findByRole("dialog", { name: "Abandon Issue#1" });
  fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), { target: { value: "Superseded by Issue#9" } });
  fireEvent.click(within(dialog).getByRole("button", { name: /Abandon/ }));
  await waitFor(() => expect(prop("Status").textContent).toBe("Abandoned"));
  expect(server.writes()).toEqual([
    { call: "PUT status", body: { value: "abandoned", mode: "cas", expected: "active", reason: "Superseded by Issue#9" } },
  ]);
});

test("a cancelled invalidation writes nothing", async () => {
  const server = serveRow(row("Issue", 1, { status: "abandoned" }));
  await open(server);
  await pick("Status", "Invalid");
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(server.writes()).toEqual([]);
});

test("a judgement change asks for a reason, then writes by compare-and-set", async () => {
  const server = serveRow(row("Belief", 1, { judgement: "unproven", confidence: 0.5 }));
  await open(server);
  await pick("Judgement", "Proven");
  const dialog = await screen.findByRole("dialog", { name: "Mark Belief#1 Proven" });
  fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), { target: { value: "Experiment#3" } });
  fireEvent.keyDown(within(dialog).getByRole("textbox"), { key: "Enter", metaKey: true });
  await waitFor(() => expect(prop("Judgement").textContent).toBe("Proven"));
  expect(server.writes()).toEqual([
    { call: "PUT judgement", body: { value: "proven", mode: "cas", expected: "unproven", reason: "Experiment#3" } },
  ]);
});

test("priority takes a band", async () => {
  const server = serveRow(row("Issue", 1, { priority: 20 }));
  await open(server);
  await pick("Priority", "P1 High");
  await waitFor(() => expect(prop("Priority").textContent).toBe("P1 High10"));
  expect(server.writes()).toEqual([{ call: "PUT priority", body: { value: 10 } }]);
});

test("priority takes an exact number (COLD-10)", async () => {
  const server = serveRow(row("Issue", 1, { priority: 10 }));
  await open(server);
  await pick("Priority", /^Exact number…/);
  const exact = await screen.findByRole("spinbutton", { name: "Exact priority" });
  await waitFor(() => expect(document.activeElement).toBe(exact));
  fireEvent.change(exact, { target: { value: "15" } });
  fireEvent.submit(exact.closest("form")!);
  await waitFor(() => expect(prop("Priority").textContent).toBe("P1 High15"));
  expect(server.writes()).toEqual([{ call: "PUT priority", body: { value: 15 } }]);
});

test("priority takes No priority", async () => {
  const server = serveRow(row("Issue", 1, { priority: 15 }));
  await open(server);
  await pick("Priority", "No priority");
  await waitFor(() => expect(prop("Priority").textContent).toBe("No priority"));
  expect(server.writes()).toEqual([{ call: "DELETE priority", body: {} }]);
});

test.each(["Status", "Priority"])("a digit typed in the %s menu's search box is a digit, not a pick (COLD-15)", async (label) => {
  const server = serveRow(row("Issue", 1, { priority: 20 }));
  await open(server);
  fireEvent.click(propButton(label));
  const search = await screen.findByRole("combobox");
  await userEvent.type(search, "2");
  expect((search as HTMLInputElement).value).toBe("2");
  expect(screen.getByRole("listbox")).toBeTruthy();
  fireEvent.keyDown(search, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("listbox")).toBeNull());
  expect(server.writes()).toEqual([]);
});

test("labels change one element per PATCH, ticks made while one saves wait their turn", async () => {
  const server = serveRow(row("Issue", 1, { labels: ["infra"] }));
  // A label on another loaded row is offered too.
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(["inquiries", "list"], [row("Issue", 2, { labels: ["ops"] })]);
  await open(server, undefined, queryClient);
  await pick("Labels", "ops", "infra");
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "web ui" } });
  fireEvent.click(screen.getByRole("option", { name: "Create label “web ui”" }));
  await waitFor(() => expect(server.writes()).toHaveLength(3));
  await waitFor(() => expect(prop("Labels").textContent).toBe("opsweb ui"));
  expect(server.writes()).toEqual([
    { call: "PATCH labels", body: { op: "add", value: "ops" } },
    { call: "PATCH labels", body: { op: "sub", value: "infra" } },
    { call: "PATCH labels", body: { op: "add", value: "web ui" } },
  ]);
  const options = screen.getAllByRole("option").map((option) => [option.textContent, option.getAttribute("aria-selected")]);
  expect(options).toEqual([
    ["infra", "false"],
    ["ops", "true"],
    ["web ui", "true"],
  ]);
});

test("removing an Issue's last type is a DELETE checked at save, so a type someone added meanwhile stays (R4-F02)", async () => {
  const server = serveRow(row("Issue", 1, { issue_kind: ["bug"] }));
  await open(server);
  server.change("issue_kind", ["bug", "feature"], "josh@example.com");
  await pick("Type", "bug");
  // The server will not empty an Issue's type by PATCH, and a DELETE would clear the added type too.
  const dialog = await screen.findByRole("alertdialog", { name: "Type changed" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Keep theirs" }));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  expect(server.writes()).toEqual([]);
  expect(server.self.issue_kind).toEqual(["bug", "feature"]);
});

test("with nothing added meanwhile, an Issue's last type goes with one DELETE", async () => {
  const server = serveRow(row("Issue", 1, { issue_kind: ["bug"] }));
  await open(server);
  await pick("Type", "bug");
  await waitFor(() => expect(server.self.issue_kind).toBeNull());
  expect(server.writes()).toEqual([{ call: "DELETE issue_kind", body: {} }]);
});

test("a byline removes one of two like-named authors, as the server does, and shows the other (E8-06)", async () => {
  const server = serveRow(row("Paper", 1, { authors: ["Ada Lovelace", "Alan Turing", "Ada Lovelace"] }));
  await open(server);
  await pick("Authors", "Ada Lovelace");
  expect(screen.getByRole("option", { name: "Ada Lovelace" }).getAttribute("aria-selected")).toBe("true");
  await waitFor(() => expect(server.self.authors).toEqual(["Alan Turing", "Ada Lovelace"]));
  await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  expect(screen.getByRole("option", { name: "Ada Lovelace" }).getAttribute("aria-selected")).toBe("true");
  expect(server.writes()).toEqual([{ call: "PATCH authors", body: { op: "sub", value: "Ada Lovelace" } }]);
});

test("owner: Me writes the email by compare-and-set, and a 409 opens the conflict dialog", async () => {
  const server = serveRow(row("Issue", 1));
  await open(server);
  server.change("owner", "josh", "josh@example.com");
  await pick("Owner", ME);
  const dialog = await screen.findByRole("alertdialog", { name: "Owner changed" });
  expect(within(dialog).getByText("josh@example.com changed this since you saw it. Save yours anyway?")).toBeTruthy();
  fireEvent.click(within(dialog).getByRole("button", { name: "Save mine" }));
  await waitFor(() => expect(prop("Owner").querySelector(".t")?.textContent).toBe(PROFILE.email));
  expect(server.writes().map(({ body }) => body)).toEqual([
    { value: PROFILE.email, mode: "cas", expected: null },
    { value: PROFILE.email, mode: "cas", expected: "josh" },
  ]);
});

test("subscribers add me by email", async () => {
  const server = serveRow(row("Issue", 1, { subscribers: ["Agent"] }));
  await open(server);
  await pick("Subscribers", ME);
  await waitFor(() => expect(server.writes()).toHaveLength(1));
  await closeMenu("Subscribers");
  expect(server.writes()).toEqual([{ call: "PATCH subscribers", body: { op: "add", value: PROFILE.email } }]);
});

test("the account takes a typed email", async () => {
  const server = serveRow(row("Issue", 1, { subscribers: ["Agent"] }));
  await open(server);
  await pick("Account");
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "grace@example.com" } });
  fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
  await waitFor(() => expect(prop("Account").textContent).toBe("grace@example.com"));
  expect(server.writes()).toEqual([{ call: "PUT account", body: { value: "grace@example.com" } }]);
});

test("any other field edits in a form typed by its value, with an optional reason", async () => {
  const server = serveRow(row("Paper", 1, { venue: "NeurIPS" }));
  await open(server);
  fireEvent.click(propButton("Venue"));
  const venue = await screen.findByRole("textbox", { name: "Venue" });
  fireEvent.change(venue, { target: { value: "ICML" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Reason" }), { target: { value: "Camera-ready" } });
  fireEvent.submit(venue.closest("form")!);
  await waitFor(() => expect(prop("Venue").textContent).toBe("ICML"));
  expect(server.writes()).toEqual([{ call: "PUT venue", body: { value: "ICML", reason: "Camera-ready" } }]);
});

test("a date field edits in a date input", async () => {
  const server = serveRow(row("Paper", 1, { venue: "NeurIPS" }));
  await open(server);
  fireEvent.click(propButton("Published"));
  const date = await screen.findByLabelText("Published");
  expect(date.getAttribute("type")).toBe("date");
  fireEvent.change(date, { target: { value: "2024-01-05" } });
  fireEvent.submit(date.closest("form")!);
  await waitFor(() => expect(prop("Published").textContent).toBe("Jan 5, 2024"));
  expect(server.writes()).toEqual([{ call: "PUT publish_date", body: { value: "2024-01-05T00:00:00+00:00" } }]);
});

test("a kind's list field is a multi-select", async () => {
  const server = serveRow(row("Paper", 1, { authors: ["Ada Lovelace"] }));
  await open(server);
  await pick("Authors");
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "Alan Turing" } });
  fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
  await waitFor(() => expect(prop("Authors").textContent).toBe("Ada LovelaceAlan Turing"));
  await closeMenu("Authors");
  expect(server.writes()).toEqual([{ call: "PATCH authors", body: { op: "add", value: "Alan Turing" } }]);
});

test("a kind's closed set is a menu", async () => {
  const server = serveRow(row("Paper", 1, { authors: ["Ada Lovelace"] }));
  await open(server);
  await pick("Type", "inproceedings");
  await waitFor(() => expect(prop("Type").textContent).toBe("inproceedings"));
  expect(server.writes()).toEqual([{ call: "PUT publication_type", body: { value: "inproceedings" } }]);
});

test("the server's message shows next to the control, one line per field for a 422", async () => {
  const server = serveRow(row("Belief", 1, { judgement: "unproven", confidence: 0.5 }));
  server.answers.push(() =>
    Response.json({ detail: [{ loc: ["body", "value"], msg: "Input should be less than or equal to 1", type: "x" }] }, { status: 422 }),
  );
  await open(server);
  fireEvent.click(propButton("Author conf."));
  const input = await screen.findByRole("spinbutton", { name: "Author conf." });
  fireEvent.change(input, { target: { value: "1.5" } });
  fireEvent.submit(input.closest("form")!);
  expect((await screen.findByRole("alert")).textContent).toBe("value: Input should be less than or equal to 1");
  expect(screen.getByRole("spinbutton", { name: "Author conf." })).toBeTruthy();
  expect(server.writes()).toEqual([{ call: "PUT confidence", body: { value: 1.5 } }]);
});

test("a viewer sees values without editors; offline, the editors are off", async () => {
  const server = serveRow(row("Issue", 1, { description: "Body" }));
  await open(server, { profile: { ...PROFILE, role: "viewer" } });
  const panel = screen.getByRole("complementary", { name: "Properties" });
  expect(within(panel).queryAllByRole("button")).toEqual([]);
  expect(within(screen.getByRole("heading", { level: 1 })).queryByRole("button")).toBeNull();
  expect(screen.queryByRole("button", { name: "Edit Description" })).toBeNull();
  cleanup();

  vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  await open(serveRow(row("Issue", 1, { description: "Body" })));
  const buttons = within(screen.getByRole("complementary", { name: "Properties" })).getAllByRole("button");
  expect(buttons.length).toBeGreaterThan(5);
  expect(buttons.every((button) => button.hasAttribute("disabled"))).toBe(true);
  expect(within(screen.getByRole("heading", { level: 1 })).getByRole("button").hasAttribute("disabled")).toBe(true);
  act(() => {
    fireEvent.click(screen.getByText("Body"));
  });
  expect(screen.queryByRole("textbox", { name: "Description" })).toBeNull();
});

test("a form saves against the value it opened on: one changed while it is open asks first (F5-03)", async () => {
  const server = serveRow(row("Paper", 1, { venue: "NeurIPS" }));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await open(server, undefined, queryClient);
  fireEvent.click(propButton("Venue"));
  const venue = await screen.findByRole("textbox", { name: "Venue" });
  fireEvent.change(venue, { target: { value: "AAAI" } });
  server.change("venue", "ICML", "josh@example.com");
  await act(() => queryClient.refetchQueries({ queryKey: ["detail", server.self.id] }));
  await waitFor(() => expect(prop("Venue").textContent).toBe("ICML"));
  fireEvent.submit(venue.closest("form")!);
  const dialog = await screen.findByRole("alertdialog", { name: "Venue changed" });
  expect([...dialog.querySelectorAll("dd")].map((part) => part.textContent)).toEqual(["ICML", "AAAI"]);
  expect(server.writes()).toEqual([]);
});

test("a reason asked for saves against the value the pick was made on (F5-03)", async () => {
  const server = serveRow(row("Issue", 1));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await open(server, undefined, queryClient);
  await pick("Status", "Abandoned");
  const dialog = await screen.findByRole("dialog", { name: "Abandon Issue#1" });
  server.change("status", "complete", "josh@example.com");
  await act(() => queryClient.refetchQueries({ queryKey: ["detail", server.self.id] }));
  // The open dialog hides the panel from role queries.
  await waitFor(() => expect(document.querySelector('[data-field="status"]')?.textContent).toBe("StatusComplete"));
  fireEvent.click(within(dialog).getByRole("button", { name: /Abandon/ }));
  expect(await screen.findByRole("alertdialog", { name: "Status changed" })).toBeTruthy();
  expect(server.writes()).toEqual([{ call: "PUT status", body: { value: "abandoned", mode: "cas", expected: "active" } }]);
});

test("a form saved unchanged writes nothing, though its input cannot show the stored seconds (BR-04)", async () => {
  const server = serveRow(row("AgentSession", 1, { started: "2026-09-20T10:05:42+00:00" }));
  await open(server);
  fireEvent.click(propButton("Started"));
  const started = await screen.findByLabelText("Started");
  fireEvent.submit(started.closest("form")!);
  await waitFor(() => expect(screen.queryByLabelText("Started", { selector: "input" })).toBeNull());
  expect(server.writes()).toEqual([]);
});

test("a form open when the session ends is kept, and reopens with its text on the next load (RV-01)", async () => {
  localStorage.clear();
  const server = serveRow(row("Paper", 1, { venue: "NeurIPS" }));
  const session = new Session(() => {});
  await open(server, { session });
  fireEvent.click(propButton("Venue"));
  fireEvent.change(await screen.findByRole("textbox", { name: "Venue" }), { target: { value: "ICML, camera-ready" } });
  act(() => session.leaveForLogin());
  cleanup();

  await open(server);
  expect(await screen.findByRole("textbox", { name: "Venue" })).toHaveProperty("value", "ICML, camera-ready");
  expect(localStorage.length).toBe(0);
});

test("a form already open when the browser goes offline cannot save (CR-W02)", async () => {
  const server = serveRow(row("Paper", 1, { venue: "NeurIPS" }));
  await open(server);
  fireEvent.click(propButton("Venue"));
  const venue = await screen.findByRole("textbox", { name: "Venue" });
  fireEvent.change(venue, { target: { value: "ICML" } });
  vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  act(() => {
    dispatchEvent(new Event("offline"));
  });
  expect(screen.getByRole("button", { name: "Save" }).hasAttribute("disabled")).toBe(true);
  fireEvent.submit(venue.closest("form")!);
  await act(() => new Promise((resolve) => setTimeout(resolve)));
  expect(server.writes()).toEqual([]);
  // Back online: the query cache's online state is global, and other tests read.
  vi.restoreAllMocks();
  act(() => {
    dispatchEvent(new Event("online"));
  });
});

test("a key typed while an input method composes does not confirm the reason dialog (REV-D4-01's class)", async () => {
  const server = serveRow(row("Issue", 1));
  await open(server);
  await pick("Status", "Abandoned");
  const dialog = await screen.findByRole("dialog", { name: "Abandon Issue#1" });
  fireEvent.keyDown(within(dialog).getByRole("textbox"), { key: "Enter", metaKey: true, isComposing: true });
  expect(screen.getByRole("dialog", { name: "Abandon Issue#1" })).toBeTruthy();
  expect(server.writes()).toEqual([]);
});
