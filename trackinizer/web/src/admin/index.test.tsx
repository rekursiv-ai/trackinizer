import { QueryClient } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { FAST, profile, renderScreen, serveAccount, user } from "../settings/testing";
import { AdminView } from ".";

const ADMIN = profile("admin");
const ME = user("u-ada", "ada@example.com", { name: "Ada", role: "admin" });
const JOSH = user("u-josh", "josh@example.com", { name: "Josh", role: "admin" });
const MING = user("u-ming", "ming@example.com", { name: "Ming" });
const INTERN = user("u-intern", "intern@example.com", { name: "Intern", role: "viewer", status: "disabled" });
const WILDCARD = { email_or_pattern: "*@example.com", role: "writer" as const, added_by: null, added_at: "2026-09-01T10:00:00+00:00" };

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/**
 * The table row whose cell reads `email`. Found by text: naming every cell by
 * role, selects and all, took 10-50 ms a lookup.
 */
function rowOf(email: string) {
  return within(screen.getByText(email, { selector: "td" }).closest("tr")!);
}

/**
 * `rowOf(email)`, once the list shows it. A control is looked for in its row:
 * a query over the whole page runs again on every change while a dialog closes.
 */
async function shownRow(email: string) {
  await screen.findByText(email, { selector: "td" });
  return rowOf(email);
}

test("a non-admin who follows a link to Admin is told it is for admins, and nothing is requested", async () => {
  // A writer is refused too: visibility.test.tsx follows the link as one.
  const server = serveAccount(profile("viewer"));
  renderScreen(<AdminView />, profile("viewer"));
  expect(screen.getByRole("heading", { name: "Admins only" })).toBeTruthy();
  expect(screen.getByText("Users and the allowlist are managed by admins. Ask an admin for access.")).toBeTruthy();
  expect(screen.queryByRole("table")).toBeNull();
  // The view refetches the profile as it opens; by then an admin read would have gone too.
  await waitFor(() => expect(server.reads("/api/me/profile")).toBe(1), FAST);
  expect(server.sent.filter((request) => request.path.startsWith("/api/admin/"))).toEqual([]);
});

test("the users list shows your own row read-only, and a disabled user dimmed with Enable", async () => {
  serveAccount(ADMIN, { users: [ME, JOSH, MING, INTERN] });
  renderScreen(<AdminView />, ADMIN);
  await screen.findByText("ming@example.com", { selector: "td" });
  const me = rowOf("ada@example.com");
  expect(me.queryByRole("combobox")).toBeNull();
  expect(me.queryByRole("button")).toBeNull();
  expect(me.getByText("you")).toBeTruthy();
  expect(rowOf("intern@example.com").getByRole("button", { name: "Enable intern@example.com" })).toBeTruthy();
  expect(screen.getByText("intern@example.com", { selector: "td" }).closest("tr")!.className).toBe("dim");
});

test("changing another user's role is one PUT", async () => {
  const server = serveAccount(ADMIN, { users: [ME, JOSH, MING, INTERN] });
  renderScreen(<AdminView />, ADMIN);
  await screen.findByText("ming@example.com", { selector: "td" });
  const picker = rowOf("ming@example.com").getByRole("combobox", { name: "Role of ming@example.com" }) as HTMLSelectElement;
  expect(within(picker).getAllByRole("option").map((option) => option.textContent)).toEqual(["viewer", "writer", "admin"]);
  fireEvent.change(picker, { target: { value: "viewer" } });
  await screen.findByText("Ming: role set to viewer");
  expect(server.writes()).toEqual([{ call: "PUT /api/admin/users/u-ming/role", body: { role: "viewer" } }]);
  expect(picker.value).toBe("viewer");
});

test("the server's 409 shows next to the role: the last active admin cannot lose it", async () => {
  const server = serveAccount(ADMIN, { users: [JOSH] });
  renderScreen(<AdminView />, ADMIN);
  const picker = (await shownRow("josh@example.com")).getByRole("combobox", { name: "Role of josh@example.com" }) as HTMLSelectElement;
  fireEvent.change(picker, { target: { value: "writer" } });
  const row = rowOf("josh@example.com");
  expect((await row.findByRole("alert")).textContent).toBe("last_admin: refusing to leave the org without an active admin.");
  expect(picker.value).toBe("admin");
  expect(server.writes()).toHaveLength(1);
});

test("disable asks first, then posts", async () => {
  const server = serveAccount(ADMIN, { users: [ME, MING] });
  renderScreen(<AdminView />, ADMIN);
  fireEvent.click((await shownRow("ming@example.com")).getByRole("button", { name: "Disable ming@example.com" }));
  const dialog = screen.getByRole("alertdialog", { name: "Disable Ming?" });
  expect(dialog.textContent).toContain("every token they hold is revoked");
  expect(server.writes()).toEqual([]);
  fireEvent.click(within(dialog).getByRole("button", { name: "Disable" }));
  await rowOf("ming@example.com").findByRole("button", { name: "Enable ming@example.com" });
  expect(server.writes()).toEqual([{ call: "POST /api/admin/users/u-ming/disable", body: undefined }]);
});

test("enable does not ask", async () => {
  const server = serveAccount(ADMIN, { users: [ME, { ...MING, status: "disabled" }] });
  renderScreen(<AdminView />, ADMIN);
  fireEvent.click((await shownRow("ming@example.com")).getByRole("button", { name: "Enable ming@example.com" }));
  await rowOf("ming@example.com").findByRole("button", { name: "Disable ming@example.com" });
  expect(server.writes()).toEqual([{ call: "POST /api/admin/users/u-ming/enable", body: undefined }]);
});

test("delete asks first; Cancel sends nothing, and Delete user removes the row", async () => {
  const server = serveAccount(ADMIN, { users: [ME, MING] });
  renderScreen(<AdminView />, ADMIN);
  fireEvent.click((await shownRow("ming@example.com")).getByRole("button", { name: "Delete ming@example.com" }));
  fireEvent.click(within(screen.getByRole("alertdialog", { name: "Delete Ming?" })).getByRole("button", { name: "Cancel" }));
  expect(server.writes()).toEqual([]);
  fireEvent.click(rowOf("ming@example.com").getByRole("button", { name: "Delete ming@example.com" }));
  fireEvent.click(within(screen.getByRole("alertdialog", { name: "Delete Ming?" })).getByRole("button", { name: "Delete user" }));
  await waitFor(() => expect(screen.queryByText("ming@example.com", { selector: "td" })).toBeNull());
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(server.writes()).toEqual([{ call: "DELETE /api/admin/users/u-ming", body: undefined }]);
});

/** Open Admin over an allowlist holding `*@example.com`; returns the server and the add form's parts. */
async function openAllowlist() {
  const server = serveAccount(ADMIN, { users: [ME], allowlist: [WILDCARD] });
  renderScreen(<AdminView />, ADMIN);
  const form = await screen.findByRole("form", { name: "Add to the allowlist" });
  const field = within(form).getByRole("textbox", { name: "Email or pattern" }) as HTMLInputElement;
  return { server, form, field };
}

test("an allowlist add sends the entry as typed, and the list shows it as the server stores it", async () => {
  const { server, form, field } = await openAllowlist();
  fireEvent.change(field, { target: { value: " Advisor@University.edu " } });
  fireEvent.change(within(form).getByRole("combobox", { name: "Role for new entry" }), { target: { value: "viewer" } });
  fireEvent.click(within(form).getByRole("button", { name: "Add" }));
  await screen.findByText("advisor@university.edu", { selector: "td" });
  expect(field.value).toBe("");
  expect(screen.getByText("Added advisor@university.edu as viewer")).toBeTruthy();
  expect(server.writes()).toEqual([
    { call: "POST /api/admin/allowlist", body: { email_or_pattern: " Advisor@University.edu ", role: "viewer" } },
  ]);
});

test("an allowlist add refused as a duplicate (409) or a blank entry (422) says why, and keeps the entry", async () => {
  const { server, form, field } = await openAllowlist();
  fireEvent.change(within(form).getByRole("combobox", { name: "Role for new entry" }), { target: { value: "viewer" } });
  fireEvent.change(field, { target: { value: "*@example.com" } });
  fireEvent.click(within(form).getByRole("button", { name: "Add" }));
  expect((await within(form).findByRole("alert")).textContent).toBe("unique constraint violated");
  expect(field.value).toBe("*@example.com");
  fireEvent.change(field, { target: { value: "   " } });
  fireEvent.click(within(form).getByRole("button", { name: "Add" }));
  await waitFor(() => expect(within(form).getByRole("alert").textContent).toBe("allowlist entry cannot be blank"));
  expect(server.writes()).toEqual([
    { call: "POST /api/admin/allowlist", body: { email_or_pattern: "*@example.com", role: "viewer" } },
    { call: "POST /api/admin/allowlist", body: { email_or_pattern: "   ", role: "viewer" } },
  ]);
});

test("an allowlist entry's role changes, and the entry is removed after asking", async () => {
  const { server } = await openAllowlist();
  await screen.findByText("*@example.com", { selector: "td" });
  fireEvent.change(rowOf("*@example.com").getByRole("combobox", { name: "Role for *@example.com" }), { target: { value: "admin" } });
  await screen.findByText("*@example.com: role set to admin");
  fireEvent.click(rowOf("*@example.com").getByRole("button", { name: "Remove *@example.com" }));
  const dialog = screen.getByRole("alertdialog", { name: "Remove *@example.com?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
  await waitFor(() => expect(screen.queryByText("*@example.com", { selector: "td" })).toBeNull());
  expect(server.writes()).toEqual([
    { call: "PUT /api/admin/allowlist/*%40example.com/role", body: { role: "admin" } },
    { call: "DELETE /api/admin/allowlist/*%40example.com", body: undefined },
  ]);
});

/**
 * Open Admin's allowlist form, and a typist: typing, unlike a change event, is
 * refused by a read-only field.
 */
async function openAddForm() {
  const server = serveAccount(ADMIN, { users: [ME] });
  const typist = userEvent.setup();
  renderScreen(<AdminView />, ADMIN);
  const form = await screen.findByRole("form", { name: "Add to the allowlist" });
  const field = within(form).getByRole("textbox", { name: "Email or pattern" }) as HTMLInputElement;
  const role = within(form).getByRole("combobox", { name: "Role for new entry" }) as HTMLSelectElement;
  const add = within(form).getByRole("button", { name: "Add" });
  const submit = (entry: string) => {
    fireEvent.change(field, { target: { value: entry } });
    fireEvent.click(add);
  };
  return { server, typist, form, field, role, add, submit };
}

test("an add's entry and role stay as sent while it is pending, and a draft typed meanwhile is not taken", async () => {
  const { server, typist, form, field, role, submit } = await openAddForm();
  const release = server.hold();
  submit("a@example.com");
  await within(form).findByText("Saving…");
  await typist.type(field, "b");
  expect([field.value, field.readOnly, role.disabled]).toEqual(["a@example.com", true, true]);
  release();
  await screen.findByText("a@example.com", { selector: "td" });
  expect(field.value).toBe("");
  expect(server.writes().map(({ body }) => body)).toEqual([{ email_or_pattern: "a@example.com", role: "writer" }]);
});

test("after an add fails its entry and role stay as sent, and Retry sends what the form shows", async () => {
  const { server, typist, form, field, role, add, submit } = await openAddForm();
  server.answers.push(() => Response.json({ detail: "database unavailable" }, { status: 503 }));
  submit("c@example.com");
  const alert = await within(form).findByRole("alert");
  await typist.type(field, "d");
  expect([field.value, field.readOnly, role.disabled, add.getAttribute("aria-disabled")]).toEqual(["c@example.com", true, true, "true"]);
  fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
  await screen.findByText("c@example.com", { selector: "td" });
  expect(server.writes().map(({ body }) => body)).toEqual([
    { email_or_pattern: "c@example.com", role: "writer" },
    { email_or_pattern: "c@example.com", role: "writer" },
  ]);
});

test("Discard after a failed add frees its entry and role", async () => {
  const { server, typist, form, field, role, submit } = await openAddForm();
  server.answers.push(() => Response.json({ detail: "database unavailable" }, { status: 503 }));
  submit("e@example.com");
  fireEvent.click(within(await within(form).findByRole("alert")).getByRole("button", { name: "Discard" }));
  await typist.type(field, "f");
  expect([field.value, field.readOnly, role.disabled]).toEqual(["e@example.comf", false, false]);
});

test("a failed refetch keeps users and allowlist entries on screen, beside the error and Retry", async () => {
  const server = serveAccount(ADMIN, { users: [ME, MING], allowlist: [WILDCARD] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderScreen(<AdminView />, ADMIN, queryClient);
  await screen.findByText("ming@example.com", { selector: "td" });
  await screen.findByText("*@example.com", { selector: "td" });
  server.failing.add("/api/admin/users");
  server.failing.add("/api/admin/allowlist");
  await act(() => queryClient.refetchQueries({ queryKey: ["admin"] }));
  for (const [section, cell] of [
    ["Users", "ming@example.com"],
    ["Allowlist", "*@example.com"],
  ] as const) {
    const region = within(screen.getByRole("region", { name: section }));
    expect((await region.findByRole("alert")).textContent).toContain("database unavailable");
    expect(region.getByRole("cell", { name: cell })).toBeTruthy();
  }
});

test("a failed profile refetch shows its error and Retry", async () => {
  const server = serveAccount(ADMIN, { users: [ME] });
  server.failing.add("/api/me/profile");
  renderScreen(<AdminView />, ADMIN);
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain("database unavailable");
  server.failing.clear();
  fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(server.reads("/api/me/profile")).toBe(2);
});

test("a 403 on an admin write says the role cannot make it", async () => {
  const server = serveAccount(ADMIN, { users: [ME, MING] });
  server.answers.push(() => Response.json({ detail: "admin role required" }, { status: 403 }));
  renderScreen(<AdminView />, ADMIN);
  fireEvent.change((await shownRow("ming@example.com")).getByRole("combobox", { name: "Role of ming@example.com" }), { target: { value: "admin" } });
  expect((await rowOf("ming@example.com").findByRole("alert")).textContent).toBe("Your role cannot make this change. admin role required");
});
