import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { InquiryRow } from "../api/inquiries";
import type { Profile } from "../api/me";
import { type Sent, stubFetch } from "../api/testing";
import { MetaContext, ProfileContext } from "../app/boot";
import { createQueryClient } from "../app/queryClient";
import { Session, SessionContext } from "../app/session";
import { CommandRegistry, CommandRegistryContext, Shortcuts } from "../commands/registry";
import { CreateView } from "../create";
import { META, PROFILE, renderDetail, row, uuid } from "../detail/testing";
import { prop, propButton, serveRow, stubLayout } from "../editors/testing";
import { ListView } from "../lists";
import { RouterProvider } from "../router/router";
import { storageKey } from "../state/store";
import { ToastProvider } from "../ui/toast";

const ADMIN: Profile = { ...PROFILE, role: "admin" };

beforeEach(() => {
  history.replaceState(null, "", "#/ref/Issue/1");
  sessionStorage.clear();
  localStorage.clear();
  stubLayout();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** Render the served inquiry's detail, once it has loaded; returns its cache. */
async function openDetail(server: ReturnType<typeof serveRow>, profile = PROFILE) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderDetail({ id: server.self.id }, queryClient, { profile });
  await screen.findByRole("heading", { level: 1 });
  return queryClient;
}

/** Click `button`, then the menu option named `option`. */
async function choose(button: HTMLElement, option: string | RegExp) {
  fireEvent.click(button);
  fireEvent.click(await screen.findByRole("option", { name: option }));
}

/** Fill the open New person dialog's fields, by label. */
function fill(dialog: HTMLElement, fields: { [label: string]: string }) {
  for (const [label, value] of Object.entries(fields)) {
    fireEvent.change(within(dialog).getByRole("textbox", { name: label }), { target: { value } });
  }
}

/** The people this browser keeps for the signed-in user. */
function kept() {
  return JSON.parse(localStorage.getItem(storageKey(PROFILE.email)) ?? "{}").people;
}

test("New person adds an owner: the email is written by compare-and-set, and the picker names them, after a reload too", { tags: ["manual"] }, async () => {
  const server = serveRow(row("Issue", 1));
  await openDetail(server);
  await choose(propButton("Owner"), "New person…");
  const dialog = await screen.findByRole("dialog", { name: "New owner" });
  expect(within(dialog).queryByRole("checkbox")).toBeNull();
  fill(dialog, { Name: "Grace Hopper", "Email (optional)": "Grace@Example.com" });
  fireEvent.click(within(dialog).getByRole("button", { name: /^Add as owner/ }));
  await waitFor(() => expect(prop("Owner").querySelector(".t")?.textContent).toBe("grace@example.com"));
  expect(server.writes()).toEqual([{ call: "PUT owner", body: { value: "grace@example.com", mode: "cas", expected: null } }]);
  expect(kept()).toEqual({ "grace@example.com": { name: "Grace Hopper", type: "person" } });
  cleanup();

  await openDetail(server);
  fireEvent.click(propButton("Owner"));
  const grace = await screen.findByRole("option", { name: /^Grace Hopper/ });
  expect(grace.querySelector(".hint")?.textContent).toBe("grace@example.com");
  expect(grace.getAttribute("aria-selected")).toBe("true");
});

test("New agent subscribes an agent by its handle", async () => {
  const server = serveRow(row("Issue", 1, { subscribers: ["Agent"] }));
  await openDetail(server);
  await choose(propButton("Subscribers"), "New agent…");
  const dialog = await screen.findByRole("dialog", { name: "New subscriber" });
  fill(dialog, { Handle: "craftax-arm" });
  expect(within(dialog).getByText("New agent")).toBeTruthy();
  fireEvent.click(within(dialog).getByRole("button", { name: /^Add as subscriber/ }));
  await waitFor(() => expect(server.writes()).toHaveLength(1));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(server.writes()).toEqual([{ call: "PATCH subscribers", body: { op: "add", value: "craftax-arm" } }]);
  expect(Object.keys(kept())).toEqual(["craftax-arm"]);
});

test("typed text opens the New agent dialog as an agent, and ⌘↵ adds it", async () => {
  const server = serveRow(row("Issue", 1, { subscribers: ["Agent"] }));
  await openDetail(server);
  fireEvent.click(propButton("Subscribers"));
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "arc-arm" } });
  fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
  const typed = await screen.findByRole("dialog", { name: "New subscriber" });
  expect(within(typed).getByRole("button", { name: "Agent" }).getAttribute("aria-pressed")).toBe("true");
  expect(within(typed).getByRole("textbox", { name: "Handle" })).toHaveProperty("value", "arc-arm");
  fireEvent.keyDown(within(typed).getByRole("textbox", { name: "Handle" }), { key: "Enter", metaKey: true });
  await waitFor(() => expect(server.writes()).toHaveLength(1));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(server.writes()).toEqual([{ call: "PATCH subscribers", body: { op: "add", value: "arc-arm" } }]);
  expect(Object.keys(kept())).toEqual(["arc-arm"]);
});

test("one added through the dialog is only ever added: an agent subscribed already stays", async () => {
  const server = serveRow(row("Issue", 1, { subscribers: ["Agent"] }));
  await openDetail(server);
  await choose(propButton("Subscribers"), "New agent…");
  const again = await screen.findByRole("dialog", { name: "New subscriber" });
  fill(again, { Handle: "Agent" });
  fireEvent.click(within(again).getByRole("button", { name: /^Add as subscriber/ }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(server.writes()).toEqual([]);
  expect(Object.keys(kept())).toEqual(["Agent"]);
});

test("composing Enter adds nobody", async () => {
  const server = serveRow(row("Issue", 1));
  await openDetail(server);
  await choose(propButton("Owner"), "New person…");
  const dialog = await screen.findByRole("dialog", { name: "New owner" });
  fill(dialog, { Name: "Grace" });
  // ⌘↵ while an input method composes commits the text, and adds nobody.
  fireEvent.keyDown(within(dialog).getByRole("textbox", { name: "Name" }), { key: "Enter", metaKey: true, isComposing: true });
  expect(screen.getByRole("dialog", { name: "New owner" })).toBeTruthy();
  expect(server.writes()).toEqual([]);
  expect(kept()).toBeUndefined();
});

test("what the dialog cannot add says why and writes nothing; Escape closes it", async () => {
  const server = serveRow(row("Issue", 1));
  await openDetail(server);
  await choose(propButton("Owner"), "New person…");
  const dialog = await screen.findByRole("dialog", { name: "New owner" });
  fireEvent.click(within(dialog).getByRole("button", { name: /^Add as owner/ }));
  expect(within(dialog).getByRole("alert").textContent).toBe("Add a name or an email.");
  fireEvent.click(within(dialog).getByRole("button", { name: "Agent" }));
  fill(dialog, { Handle: "two words" });
  expect(within(dialog).getByText("A handle is one word, with no spaces.")).toBeTruthy();
  fireEvent.keyDown(dialog, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(server.writes()).toEqual([]);
  expect(kept()).toBeUndefined();
});

test("an owner added through the dialog is guarded against the owner shown when New person was picked", async () => {
  const server = serveRow(row("Issue", 1));
  const queryClient = await openDetail(server);
  await choose(propButton("Owner"), "New person…");
  const dialog = await screen.findByRole("dialog", { name: "New owner" });
  server.change("owner", "josh", "josh@example.com");
  await act(() => queryClient.refetchQueries({ queryKey: ["detail", server.self.id] }));
  // The open dialog hides the panel from role queries.
  await waitFor(() => expect(document.querySelector('[data-field="owner"] .t')?.textContent).toBe("josh"));
  fill(dialog, { Name: "Grace" });
  fireEvent.click(within(dialog).getByRole("button", { name: /^Add as owner/ }));
  expect(await screen.findByRole("alertdialog", { name: "Owner changed" })).toBeTruthy();
  expect(server.writes()).toEqual([{ call: "PUT owner", body: { value: "Grace", mode: "cas", expected: null } }]);
});

/**
 * Serve the admin routes over `serveRow`'s server: `users` and `entries` for the
 * two lists, and `adds` for each allowlist add in turn. Returns the admin requests.
 */
function serveAdmin(users: object[], entries: object[], adds: (() => Response)[]): Sent[] {
  const inner = globalThis.fetch;
  const sent: Sent[] = [];
  vi.stubGlobal("fetch", async (request: Request) => {
    const { pathname } = new URL(request.url);
    if (!pathname.startsWith("/api/admin/")) return inner(request);
    const text = await request.clone().text();
    sent.push({ method: request.method, path: pathname, query: "", headers: Object.fromEntries(request.headers), body: text ? JSON.parse(text) : undefined });
    if (request.method === "GET") return Response.json(pathname.endsWith("/users") ? { users } : { entries });
    return adds.shift()!();
  });
  return sent;
}

const JOSH = { id: "u2", email: "josh@example.com", name: "Josh", role: "writer", status: "active", created_at: "", last_login: null };
const PENDING = { email_or_pattern: "pending@example.com", role: "viewer", added_by: null, added_at: "" };

test("for an admin, an email that has an account or an allowlist entry says so, and offers no invite", async () => {
  const server = serveRow(row("Issue", 1));
  serveAdmin([JOSH], [PENDING], []);
  await openDetail(server, ADMIN);
  await choose(propButton("Owner"), "New person…");
  const dialog = await screen.findByRole("dialog", { name: "New owner" });
  fill(dialog, { "Email (optional)": "josh@example.com" });
  expect(await within(dialog).findByText("Matches Josh's account (writer)")).toBeTruthy();
  expect(within(dialog).queryByRole("checkbox")).toBeNull();
  fill(dialog, { "Email (optional)": "pending@example.com" });
  expect(within(dialog).getByText("On the allowlist as viewer; not signed in yet")).toBeTruthy();
  expect(within(dialog).queryByRole("checkbox")).toBeNull();
});

test("an admin invites a new person: the allowlist add is sent once, Retry resends it, and then the owner is written", async () => {
  const server = serveRow(row("Issue", 1));
  const added = { email_or_pattern: "grace@example.com", role: "writer", added_by: "u1", added_at: "" };
  const admin = serveAdmin([JOSH], [PENDING], [
    () => Response.json({ detail: "database unavailable" }, { status: 503 }),
    () => Response.json(added, { status: 201 }),
  ]);
  await openDetail(server, ADMIN);
  await choose(propButton("Owner"), "New person…");
  const dialog = await screen.findByRole("dialog", { name: "New owner" });
  fill(dialog, { Name: "Grace Hopper", "Email (optional)": "grace@example.com" });
  expect(within(dialog).getByText("No account yet")).toBeTruthy();
  fireEvent.click(within(dialog).getByRole("checkbox", { name: /^Invite to sign in/ }));
  fireEvent.click(within(dialog).getByRole("button", { name: /^Add as owner/ }));
  await within(dialog).findByRole("button", { name: "Retry" });
  expect(admin.filter(({ method }) => method === "POST")).toHaveLength(1);
  expect(server.writes()).toEqual([]);

  fireEvent.click(within(dialog).getByRole("button", { name: "Retry" }));
  // A write the server receives changes nothing on screen, so only the interval checks again.
  await waitFor(() => expect(server.writes()).toHaveLength(1), { interval: 1 });
  const posts = admin.filter(({ method }) => method === "POST");
  expect(posts.map(({ path, body }) => [path, body])).toEqual([
    ["/api/admin/allowlist", { email_or_pattern: "grace@example.com", role: "writer" }],
    ["/api/admin/allowlist", { email_or_pattern: "grace@example.com", role: "writer" }],
  ]);
  expect(posts.every(({ headers }) => !("idempotency-key" in headers))).toBe(true);
  expect(server.writes()).toEqual([{ call: "PUT owner", body: { value: "grace@example.com", mode: "cas", expected: null } }]);
  expect(await screen.findByText("Invited grace@example.com to sign in as a writer")).toBeTruthy();
});

test("a discarded invite adds nobody", async () => {
  const server = serveRow(row("Issue", 1));
  serveAdmin([], [], [() => Response.json({ detail: "database unavailable" }, { status: 503 })]);
  await openDetail(server, ADMIN);
  await choose(propButton("Owner"), "New person…");
  const dialog = await screen.findByRole("dialog", { name: "New owner" });
  fill(dialog, { "Email (optional)": "grace@example.com" });
  fireEvent.click(await within(dialog).findByRole("checkbox", { name: /^Invite to sign in/ }));
  fireEvent.click(within(dialog).getByRole("button", { name: /^Add as owner/ }));
  fireEvent.click(await within(dialog).findByRole("button", { name: "Discard" }));
  await waitFor(() => expect(within(dialog).queryByRole("button", { name: "Retry" })).toBeNull());
  expect(screen.getByRole("dialog", { name: "New owner" })).toBeTruthy();
  expect(server.writes()).toEqual([]);
  expect(kept()).toBeUndefined();
});

test("a writer is offered no invite, and nothing asks the admin routes", async () => {
  const server = serveRow(row("Issue", 1));
  const admin = serveAdmin([], [], []);
  await openDetail(server, PROFILE);
  await choose(propButton("Owner"), "New person…");
  const writers = await screen.findByRole("dialog", { name: "New owner" });
  fill(writers, { "Email (optional)": "grace@example.com" });
  expect(within(writers).queryByRole("checkbox")).toBeNull();
  expect(admin).toEqual([]);
});

/** Open the create form for `kind` at `#/new/<kind>`, over a server that creates `uuid(900)`. */
function openCreate(kind: string) {
  history.replaceState(null, "", `#/new/${kind}`);
  const sent = stubFetch((request) =>
    request.method === "GET" ? Response.json({ detail: "not found" }, { status: 404 }) : Response.json({ id: uuid(900) }, { status: 201 }),
  );
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SessionContext value={new Session(() => {})}>
        <CommandRegistryContext value={new CommandRegistry()}>
          <ToastProvider>
            <MetaContext value={META}>
              <ProfileContext value={PROFILE}>
                <RouterProvider kinds={META.kinds}>
                  <CreateView kind={kind} onClose={() => {}} />
                </RouterProvider>
              </ProfileContext>
            </MetaContext>
          </ToastProvider>
        </CommandRegistryContext>
      </SessionContext>
    </QueryClientProvider>,
  );
  return sent;
}

test("in a create form, New agent sets the owner chip and New person a subscriber; the create sends their values", { tags: ["manual"] }, async () => {
  const sent = openCreate("Issue");
  const form = () => within(screen.getByRole("dialog", { name: "New issue" }));
  fireEvent.change(form().getByRole("textbox", { name: "Title" }), { target: { value: "Ship people" } });
  await choose(form().getByRole("button", { name: "Owner: No owner" }), "New agent…");
  const agent = await screen.findByRole("dialog", { name: "New owner" });
  fill(agent, { Handle: "craftax-arm" });
  fireEvent.click(within(agent).getByRole("button", { name: /^Add as owner/ }));
  expect(await form().findByRole("button", { name: "Owner: craftax-arm" })).toBeTruthy();

  await choose(form().getByRole("button", { name: "Subscribers: No subscribers" }), "New person…");
  const person = await screen.findByRole("dialog", { name: "New subscriber" });
  fill(person, { Name: "Grace Hopper", "Email (optional)": "grace@example.com" });
  fireEvent.click(within(person).getByRole("button", { name: /^Add as subscriber/ }));
  expect(await form().findByRole("button", { name: "Subscribers: Grace Hopper" })).toBeTruthy();

  // Escape in a dialog opened over the form closes that dialog only.
  await choose(form().getByRole("button", { name: "Subscribers: Grace Hopper" }), "New person…");
  fireEvent.keyDown(await screen.findByRole("dialog", { name: "New subscriber" }), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "New subscriber" })).toBeNull());
  fireEvent.click(form().getByRole("button", { name: /^Create issue/ }));
  await waitFor(() => expect(sent.filter(({ method }) => method === "POST")).toHaveLength(1));
  expect(sent.find(({ method }) => method === "POST")!.body).toMatchObject({
    title: "Ship people",
    owner: "craftax-arm",
    subscribers: ["grace@example.com"],
  });
});

function issue(seq: number): InquiryRow {
  return {
    id: uuid(seq),
    kind: "Issue",
    seq,
    title: `Issue ${seq}`,
    status: "active",
    owner: null,
    labels: null,
    priority: null,
    marginal_cost: { agent_usd: 0, resource_usd: 0 },
    created: "2026-09-20T00:00:00+00:00",
    modified: "2026-09-20T00:00:00+00:00",
  };
}

test("in the bulk bar, New person sets every selected row's owner to their email, one compare-and-set each", { tags: ["manual"] }, async () => {
  history.replaceState(null, "", "#/list/Issue");
  const rows = [issue(1), issue(2)];
  const sent = stubFetch(async (request) => {
    const { pathname } = new URL(request.url);
    if (request.method === "GET") return Response.json(pathname === "/api/inquiries" ? rows : { detail: "not found" }, pathname === "/api/inquiries" ? {} : { status: 404 });
    return Response.json({ id: pathname.split("/")[3], change_id: "c" });
  });
  render(
    <QueryClientProvider client={createQueryClient(() => {})}>
      <CommandRegistryContext value={new CommandRegistry()}>
        <ToastProvider>
          <Shortcuts />
          <MetaContext value={META}>
            <ProfileContext value={PROFILE}>
              <RouterProvider kinds={META.kinds}>
                <ListView kind="Issue" />
              </RouterProvider>
            </ProfileContext>
          </MetaContext>
        </ToastProvider>
      </CommandRegistryContext>
    </QueryClientProvider>,
  );
  await screen.findByText("Issue 2");
  fireEvent.click(screen.getByRole("button", { name: "Select Issue#1" }));
  fireEvent.click(screen.getByRole("button", { name: "Select Issue#2" }));
  const bar = screen.getByRole("group", { name: "Bulk actions" });
  await choose(within(bar).getByRole("button", { name: "Owner" }), "New person…");
  const dialog = await screen.findByRole("dialog", { name: "New owner" });
  fill(dialog, { Name: "Grace Hopper", "Email (optional)": "grace@example.com" });
  fireEvent.click(within(dialog).getByRole("button", { name: /^Add as owner/ }));
  await waitFor(() => expect(sent.filter(({ method }) => method === "PUT")).toHaveLength(2));
  expect(sent.filter(({ method }) => method === "PUT").map(({ path, body }) => [path, body])).toEqual(
    [1, 2].map((seq) => [`/api/inquiries/${uuid(seq)}/owner`, { value: "grace@example.com", mode: "cas", expected: null }]),
  );
});
