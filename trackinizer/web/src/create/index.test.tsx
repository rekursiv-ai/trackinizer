import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { Profile } from "../api/me";
import { stubFetch } from "../api/testing";
import { MetaContext, ProfileContext } from "../app/boot";
import { Session, SessionContext } from "../app/session";
import { CommandRegistry, CommandRegistryContext } from "../commands/registry";
import { detail, META, PROFILE, row, uuid } from "../detail/testing";
import { stubLayout } from "../editors/testing";
import { cacheWith } from "../relations/testing";
import { formatRoute } from "../router/route";
import { RouterProvider, useRouter } from "../router/router";
import { ToastProvider } from "../ui/toast";
import { CreateView } from ".";

const NEW = uuid(900);
const PARENT = row("Issue", 1, { title: "Parent plan" });
const OTHER = row("Issue", 2, { title: "Other plan" });

beforeEach(() => {
  stubLayout();
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** A server that creates `NEW` for every create, or answers with `refuse`; returns the requests it got. */
function serve(refuse?: () => Response) {
  return stubFetch((request) => {
    const { pathname } = new URL(request.url);
    if (request.method === "GET") return Response.json({ detail: "not found" }, { status: 404 });
    if (refuse) return refuse();
    return Response.json(pathname.endsWith("/batch") ? { ids: [NEW] } : { id: NEW }, { status: 201 });
  });
}

/** Open the app at `hash`, where a `#/new/<Kind>` route shows the form and any other its hash. */
function open(hash: string, { profile = PROFILE, session = new Session(() => {}) }: { profile?: Profile; session?: Session } = {}) {
  history.replaceState(null, "", hash);
  return render(
    <QueryClientProvider client={cacheWith([PARENT, OTHER])}>
      <SessionContext value={session}>
        <CommandRegistryContext value={new CommandRegistry()}>
          <ToastProvider>
            <MetaContext value={META}>
              <ProfileContext value={profile}>
                <RouterProvider kinds={META.kinds}>
                  <Routed />
                </RouterProvider>
              </ProfileContext>
            </MetaContext>
          </ToastProvider>
        </CommandRegistryContext>
      </SessionContext>
    </QueryClientProvider>,
  );
}

function Routed() {
  const { route, navigate } = useRouter();
  if (route.name !== "new") return <p data-testid="route">{formatRoute(route)}</p>;
  return <CreateView kind={route.kind} onClose={() => navigate({ name: "list", kind: "Issue" }, { replace: true })} />;
}

/** The form's dialog; a chip's open menu is a dialog of its own. */
const form = () => within(screen.getByRole("dialog", { name: /^New / }));
const title = () => form().getByRole("textbox", { name: "Title" }) as HTMLInputElement;
const chips = () => [...screen.getByRole("dialog", { name: /^New / }).querySelectorAll(".chip-btn[data-field]")].map((chip) => chip.getAttribute("aria-label"));
const fields = () => [...screen.getByRole("dialog", { name: /^New / }).querySelectorAll(".cf-grid label")].map((label) => label.firstChild!.textContent);
const creates = (sent: ReturnType<typeof serve>) => sent.filter((request) => request.method === "POST");

/**
 * Close the open menu with Escape, and wait for Radix to hand focus back to its
 * trigger: a menu opened before then would lose focus to it, and close.
 */
async function closeMenu() {
  fireEvent.keyDown(screen.getByRole("combobox"), { key: "Escape" });
  await act(() => new Promise((resolve) => setTimeout(resolve)));
}

/** Open the menu behind `button` and pick each option in turn. */
async function pick(button: HTMLElement, ...options: (string | RegExp)[]) {
  fireEvent.click(button);
  for (const option of options) fireEvent.click(await screen.findByRole("option", { name: option }));
}

test("each kind's form asks for its own fields and chips; the kind menu switches kinds and keeps the text", async () => {
  open("#/new/Paper");
  expect(form().getByText("New paper")).toBeTruthy();
  expect(document.activeElement).toBe(title());
  expect(fields()).toEqual(["Abstract", "Authors", "Type", "Venue", "Subvenue", "Published", "Source", "Scholar cluster", "Scholar cites"]);
  expect(chips()).toEqual(["Status: Active", "Owner: No owner", "Subscribers: No subscribers", "Labels: No labels"]);
  fireEvent.change(title(), { target: { value: "Retry jitter" } });

  await pick(form().getByRole("button", { name: "Kind: Papers" }), "Issues");
  await waitFor(() => expect(location.hash).toBe("#/new/Issue"));
  expect(title().value).toBe("Retry jitter");
  expect(fields()).toEqual(["Done when"]);
  expect(chips()).toEqual([
    "Status: Active",
    "Priority: P2 Medium",
    "Type: task",
    "Owner: No owner",
    "Subscribers: No subscribers",
    "Labels: No labels",
  ]);
});

test("a create with relations is one request, then opens the new inquiry", { tags: ["manual"] }, async () => {
  const sent = serve();
  open("#/new/Issue");
  fireEvent.change(title(), { target: { value: "Ship retry" } });
  await pick(form().getByRole("button", { name: "Add relation" }), /^Narrows…/, /Parent plan/);
  await closeMenu();
  expect(within(form().getByRole("list", { name: "Relations" })).getByRole("listitem").textContent).toBe("NarrowsIssue#1Parent plan");
  fireEvent.click(form().getByRole("button", { name: /^Create issue/ }));

  await waitFor(() => expect(screen.getByTestId("route").textContent).toBe(`#/lookup/${NEW}`));
  expect(creates(sent).map(({ path, body }) => [path, body])).toEqual([
    [
      "/api/inquiries/issue",
      {
        title: "Ship retry",
        status: "active",
        priority: 20,
        issue_kind: ["task"],
        narrows: [[PARENT.id, null]],
        idempotency_key: expect.any(String),
      },
    ],
  ]);
  expect(await screen.findByText("Created issue “Ship retry”")).toBeTruthy();
});

test("a label made in the form goes in the create", async () => {
  const sent = serve();
  open("#/new/Issue");
  fireEvent.change(title(), { target: { value: "Ship retry" } });
  fireEvent.click(form().getByRole("button", { name: "Labels: No labels" }));
  fireEvent.change(await screen.findByRole("combobox", { name: "Labels…" }), { target: { value: "list-seed" } });
  fireEvent.click(screen.getByRole("option", { name: "Create label “list-seed”" }));
  expect(form().getByRole("button", { name: "Labels: list-seed" })).toBeTruthy();
  await closeMenu();
  fireEvent.click(form().getByRole("button", { name: /^Create issue/ }));

  await waitFor(() => expect(screen.getByTestId("route").textContent).toBe(`#/lookup/${NEW}`));
  expect(creates(sent).map(({ path, body }) => [path, body])).toEqual([
    [
      "/api/inquiries/issue",
      {
        title: "Ship retry",
        status: "active",
        priority: 20,
        issue_kind: ["task"],
        labels: ["list-seed"],
        idempotency_key: expect.any(String),
      },
    ],
  ]);
});

test("the exact priority works on the draft, and its own Save creates nothing (COLD-10)", async () => {
  const sent = serve();
  open("#/new/Issue");
  fireEvent.change(title(), { target: { value: "Exact" } });
  await pick(form().getByRole("button", { name: "Priority: P2 Medium" }), /^Exact number…/);
  const exact = await screen.findByRole("spinbutton", { name: "Exact priority" });
  fireEvent.change(exact, { target: { value: "15" } });
  fireEvent.submit(exact.closest("form")!);
  expect(await screen.findByRole("button", { name: "Priority: P1 High (15)" })).toBeTruthy();
  expect(creates(sent)).toEqual([]);

  fireEvent.keyDown(title(), { key: "Enter", metaKey: true });
  // A create sent changes nothing on screen, so only the interval checks again.
  await waitFor(() => expect(creates(sent)).toHaveLength(1), { interval: 1 });
  expect(creates(sent)[0]!.body).toMatchObject({ title: "Exact", priority: 15 });
});

test("Create more keeps the form open for the next one, with the kind and chips, and a fresh key each", { tags: ["manual"] }, async () => {
  const sent = serve();
  open("#/new/Issue");
  fireEvent.click(form().getByRole("button", { name: "Create more" }));
  await pick(form().getByRole("button", { name: "Priority: P2 Medium" }), "P0 Critical");
  for (const name of ["First", "Second"]) {
    fireEvent.change(title(), { target: { value: name } });
    fireEvent.change(form().getByRole("textbox", { name: "Description" }), { target: { value: `About ${name}` } });
    fireEvent.keyDown(title(), { key: "Enter", metaKey: true });
    await waitFor(() => expect(title().value).toBe(""));
  }
  expect(location.hash).toBe("#/new/Issue");
  expect(document.activeElement).toBe(title());
  expect(form().getByRole("textbox", { name: "Description" }).textContent).toBe("");
  expect(chips()).toContain("Priority: P0 Critical");
  const bodies = creates(sent).map(({ body }) => body as { title: string; priority: number; idempotency_key: string });
  expect(bodies.map(({ title, priority }) => [title, priority])).toEqual([
    ["First", 0],
    ["Second", 0],
  ]);
  expect(bodies[0]!.idempotency_key).not.toBe(bodies[1]!.idempotency_key);
});

test("a refusal shows the server's lines and keeps the draft", async () => {
  const sent = serve(() =>
    Response.json({ detail: [{ loc: ["body", "source"], msg: "source must be a scheme-tagged identifier", type: "value_error" }] }, { status: 422 }),
  );
  open("#/new/Paper");
  fireEvent.change(title(), { target: { value: "A paper" } });
  const source = form().getByRole("textbox", { name: "Source" });
  fireEvent.change(source, { target: { value: "2405.16391" } });
  fireEvent.click(form().getByRole("button", { name: /^Create paper/ }));
  expect((await form().findByRole("alert")).textContent).toBe("source: source must be a scheme-tagged identifier");
  expect(creates(sent)).toHaveLength(1);
  expect([title().value, (source as HTMLInputElement).value]).toEqual(["A paper", "2405.16391"]);
});

test("a viewer is told creating needs a writer; offline, Create is off; AgentSession is not created here", async () => {
  open("#/new/Issue", { profile: { ...PROFILE, role: "viewer" } });
  expect(form().getByText(/Creating needs the writer role/)).toBeTruthy();
  expect(form().queryByRole("textbox", { name: "Title" })).toBeNull();
  cleanup();

  vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  open("#/new/Issue");
  fireEvent.change(title(), { target: { value: "Offline draft" } });
  expect((form().getByRole("button", { name: /^Create issue/ }) as HTMLButtonElement).disabled).toBe(true);
  vi.restoreAllMocks();
  cleanup();

  open("#/new/AgentSession");
  expect(form().getByText("Agent sessions are recorded by trax run, not created here.")).toBeTruthy();
  fireEvent.click(form().getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.getByTestId("route").textContent).toBe("#/list/Issue"));
});

test("a session that ends keeps the open draft, which the form reopens with after sign-in", { tags: ["manual"] }, async () => {
  const session = new Session(() => {});
  open("#/new/Issue", { session });
  fireEvent.change(title(), { target: { value: "Half written" } });
  await pick(form().getByRole("button", { name: "Add relation" }), /^Requires…/, /Other plan/);
  act(() => session.leaveForLogin());
  cleanup();

  open("#/new/Issue");
  expect(title().value).toBe("Half written");
  expect(within(form().getByRole("list", { name: "Relations" })).getByRole("listitem").textContent).toBe("RequiresIssue#2Other plan");
  // Reopened once: the next form starts empty.
  cleanup();
  open("#/new/Issue");
  expect(title().value).toBe("");
});

test.each([
  ["Escape", () => fireEvent.keyDown(title(), { key: "Escape" })],
  ["Cancel", () => fireEvent.click(form().getByRole("button", { name: "Cancel" }))],
])("a form closed with %s keeps its started draft for the next open; an empty one keeps nothing", (_, close) => {
  serve();
  open("#/new/Issue");
  fireEvent.change(title(), { target: { value: "Half written" } });
  close();
  expect(screen.getByTestId("route").textContent).toBe("#/list/Issue");
  cleanup();

  open("#/new/Issue");
  expect(title().value).toBe("Half written");
  fireEvent.change(title(), { target: { value: "" } });
  close();
  cleanup();
  open("#/new/Issue");
  expect(title().value).toBe("");
});

/** A server that creates `NEW` once the returned function lets its answer through. */
function serveHeld() {
  let release = () => {};
  const until = new Promise<void>((resolve) => (release = resolve));
  const sent = stubFetch(async (request) => {
    if (request.method === "GET") return Response.json({ detail: "not found" }, { status: 404 });
    await until;
    return Response.json({ id: NEW }, { status: 201 });
  });
  return { sent, release };
}

test("a form closed while its create is sent opens nothing once it lands; the toast says it landed (RV-04)", async () => {
  const server = serveHeld();
  open("#/new/Issue");
  fireEvent.change(title(), { target: { value: "Closed early" } });
  fireEvent.click(form().getByRole("button", { name: /^Create issue/ }));
  await form().findByText("Saving…");
  fireEvent.click(form().getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.getByTestId("route").textContent).toBe("#/list/Issue"));
  server.release();
  expect(await screen.findByText("Created issue “Closed early”")).toBeTruthy();
  expect(screen.getByTestId("route").textContent).toBe("#/list/Issue");
  expect(creates(server.sent)).toHaveLength(1);
  // Its draft is not kept: it landed, and reopening it would make it twice.
  cleanup();
  open("#/new/Issue");
  expect(title().value).toBe("");
});

test("while a create is sent the form takes no input, which its landing would clear (CR-W04)", async () => {
  const server = serveHeld();
  open("#/new/Issue");
  fireEvent.click(form().getByRole("button", { name: "Create more" }));
  fireEvent.change(title(), { target: { value: "First" } });
  fireEvent.keyDown(title(), { key: "Enter", metaKey: true });
  await form().findByText("Saving…");
  expect(title().readOnly).toBe(true);
  expect((form().getByRole("textbox", { name: "Description" }) as HTMLTextAreaElement).readOnly).toBe(true);
  for (const name of ["Add relation", "Labels: No labels", "Kind: Issues"]) {
    expect(form().getByRole("button", { name }).hasAttribute("disabled"), name).toBe(true);
  }
  server.release();
  expect(await screen.findByText("Created issue “First”")).toBeTruthy();
  expect(title().readOnly).toBe(false);
  expect(title().value).toBe("");
});

test("Enter or Backspace pressed while an input method composes picks nothing and goes nowhere (REV-D4-01)", async () => {
  serve();
  open("#/new/Issue");
  fireEvent.click(form().getByRole("button", { name: "Add relation" }));
  const search = await screen.findByRole("combobox", { name: "Add relation…" });
  fireEvent.keyDown(search, { key: "Enter", isComposing: true });
  expect(screen.getByRole("combobox", { name: "Add relation…" })).toBeTruthy();
  fireEvent.keyDown(search, { key: "Enter" });
  const targets = await screen.findByRole("combobox", { name: /^Narrows: search/ });
  fireEvent.keyDown(targets, { key: "Enter", isComposing: true });
  fireEvent.keyDown(targets, { key: "Backspace", isComposing: true });
  expect(screen.getByRole("combobox", { name: /^Narrows: search/ })).toBeTruthy();
  expect(screen.getAllByRole("option").every((option) => option.getAttribute("aria-selected") === "false")).toBe(true);
  expect(form().queryByRole("list", { name: "Relations" })).toBeNull();
});

test("a saved draft another build wrote in another shape is dropped, not opened (BR-10)", () => {
  localStorage.setItem(`trackinizer.v2.create.${PROFILE.email}`, JSON.stringify({ kind: "Issue", title: "Old shape" }));
  open("#/new/Issue");
  expect(title().value).toBe("");
  expect(form().queryByRole("list", { name: "Relations" })).toBeNull();
});

test("a Kind#seq the app has not loaded is listed and picked with its title", async () => {
  const far = row("Issue", 7, { title: "Gamma far" });
  stubFetch((request) => {
    const { pathname } = new URL(request.url);
    if (pathname === "/api/inquiries/Issue/7") return Response.json({ id: far.id, kind: "Issue" });
    return pathname === `/api/web/get/${far.id}` ? Response.json(detail(far)) : Response.json({ detail: "not found" }, { status: 404 });
  });
  open("#/new/Issue");
  await pick(form().getByRole("button", { name: "Add relation" }), /^Requires…/);
  fireEvent.change(screen.getByRole("combobox", { name: /^Requires: search/ }), { target: { value: "Issue#7" } });
  fireEvent.click(await screen.findByRole("option", { name: /Gamma far/ }));
  await closeMenu();
  expect(within(form().getByRole("list", { name: "Relations" })).getByRole("listitem").textContent).toBe("RequiresIssue#7Gamma far");
});

test("a UUID the app has not loaded is looked up, and can be picked (E8-04)", async () => {
  const far = row("Issue", 7, { title: "Gamma far" });
  stubFetch((request) => {
    const { pathname } = new URL(request.url);
    return pathname === `/api/web/get/${far.id}` ? Response.json(detail(far)) : Response.json({ detail: "not found" }, { status: 404 });
  });
  open("#/new/Issue");
  await pick(form().getByRole("button", { name: "Add relation" }), /^Requires…/);
  fireEvent.change(screen.getByRole("combobox", { name: /^Requires: search/ }), { target: { value: far.id } });
  fireEvent.click(await screen.findByRole("option", { name: /Gamma far/ }));
  await closeMenu();
  expect(within(form().getByRole("list", { name: "Relations" })).getByRole("listitem").textContent).toBe("RequiresIssue#7Gamma far");
});
