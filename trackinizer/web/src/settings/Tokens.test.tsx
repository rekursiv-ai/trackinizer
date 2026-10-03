import { QueryClient } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { WriteRequest } from "../writes/requests";
import { closedOver, FAST, profile, renderScreen, serveAccount, stubClipboard, token } from "./testing";
import { TokensSection } from "./Tokens";

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function openForm() {
  fireEvent.click(await screen.findByRole("button", { name: "New token" }));
  return screen.getByRole("form", { name: "New token" });
}

function optionsOf(select: HTMLElement) {
  return within(select)
    .getAllByRole("option")
    .map((option) => `${option.textContent}${(option as HTMLOptionElement).disabled ? " (disabled)" : ""}`);
}

const SECRET = "trk_new2SECRET-t2";

/** Make the token `laptop trax`, a viewer, through the form; resolves with the secret's box. */
async function makeToken() {
  const form = await openForm();
  fireEvent.change(within(form).getByRole("textbox", { name: "Label" }), { target: { value: "  laptop trax " } });
  fireEvent.change(within(form).getByRole("combobox", { name: "Role" }), { target: { value: "viewer" } });
  fireEvent.click(within(form).getByRole("button", { name: "Create token" }));
  return (await screen.findByText("Copy this secret now; it won't be shown again:")).parentElement!;
}

test("a new token's secret shows once, with Copy, and the list shows the token by its prefix", async () => {
  const writer = profile("writer");
  const server = serveAccount(writer, { tokens: [token(1)] });
  const copied = stubClipboard();
  renderScreen(<TokensSection />, writer);
  const box = await makeToken();
  expect(within(box).getByText(SECRET)).toBeTruthy();
  expect(server.writes()).toEqual([{ call: "POST /api/me/tokens", body: { name: "laptop trax", role: "viewer" } }]);
  // The list refetched and shows the new token by its prefix, not its secret.
  await screen.findByText("trk_new2…");
  fireEvent.click(within(box).getByRole("button", { name: "Copy" }));
  await waitFor(() => expect(copied).toEqual([SECRET]), FAST);
});

test("the secret never reaches storage, the caches or the console, and Done removes it for good", async () => {
  const writer = profile("writer");
  serveAccount(writer, { tokens: [token(1)] });
  const logged = (["log", "info", "warn", "error", "debug"] as const).map((level) => vi.spyOn(console, level));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderScreen(<TokensSection />, writer, queryClient);
  const box = await makeToken();
  await screen.findByText("trk_new2…");
  const everywhere = JSON.stringify([
    { ...localStorage },
    { ...sessionStorage },
    queryClient.getQueryCache().getAll().map((query) => query.state.data),
    queryClient.getMutationCache().getAll().map((mutation) => mutation.state.data),
    logged.map((spy) => spy.mock.calls),
  ]);
  expect(everywhere).not.toContain("SECRET");

  fireEvent.click(within(box).getByRole("button", { name: "Done" }));
  expect(screen.queryByText(SECRET)).toBeNull();
  // Nothing brings it back: a new form starts empty, and the section has no copy of it.
  await openForm();
  expect(document.body.textContent).not.toContain("SECRET");
  // The mutation cache keeps each request, whose `send` closes over what it sent and got.
  const requests = queryClient.getMutationCache().getAll().map((mutation) => mutation.state.variables as WriteRequest<unknown>);
  expect(requests).toHaveLength(1);
  expect(await closedOver(requests[0]!.send)).not.toContain("SECRET");
});

test("Cancel is off while a create is pending, so the secret it makes still shows", async () => {
  const writer = profile("writer");
  const server = serveAccount(writer, { tokens: [] });
  const release = server.hold();
  renderScreen(<TokensSection />, writer);
  const form = await openForm();
  fireEvent.change(within(form).getByRole("textbox", { name: "Label" }), { target: { value: "ci" } });
  fireEvent.click(within(form).getByRole("button", { name: "Create token" }));
  await within(form).findByText("Saving…");
  const cancel = within(form).getByRole("button", { name: "Cancel" }) as HTMLButtonElement;
  expect(cancel.disabled).toBe(true);
  fireEvent.click(cancel);
  release();
  await screen.findByText("trk_new1SECRET-t1");
});

test("a role picker offers only roles up to the caller's, and shows a stronger current role it cannot pick", async () => {
  const writer = profile("writer");
  serveAccount(writer, { tokens: [token(1, { name: "old admin key", role: "admin" }), token(2, { role: "viewer" })] });
  renderScreen(<TokensSection />, writer);
  const form = await openForm();
  expect(optionsOf(within(form).getByRole("combobox", { name: "Role" }))).toEqual(["viewer", "writer"]);
  expect((within(form).getByRole("combobox", { name: "Role" }) as HTMLSelectElement).value).toBe("writer");
  expect(optionsOf(screen.getByRole("combobox", { name: "Role of old admin key" }))).toEqual(["admin (disabled)", "viewer", "writer"]);
  expect(optionsOf(screen.getByRole("combobox", { name: "Role of token 2" }))).toEqual(["viewer", "writer"]);
  cleanup();

  const viewer = profile("viewer");
  serveAccount(viewer, { tokens: [] });
  renderScreen(<TokensSection />, viewer);
  expect(optionsOf(within(await openForm()).getByRole("combobox", { name: "Role" }))).toEqual(["viewer"]);
});

test("changing a token's role is one PUT, and the list then shows the stored role", async () => {
  const admin = profile("admin");
  const server = serveAccount(admin, { tokens: [token(1)] });
  renderScreen(<TokensSection />, admin);
  const picker = (await screen.findByRole("combobox", { name: "Role of token 1" })) as HTMLSelectElement;
  fireEvent.change(picker, { target: { value: "admin" } });
  expect(picker.value).toBe("admin");
  await screen.findByText("token 1: role set to admin");
  expect(server.writes()).toEqual([{ call: "PUT /api/me/tokens/t1/role", body: { role: "admin" } }]);
  expect(picker.disabled).toBe(false);
  expect(server.reads("/api/me/tokens")).toBe(2);
});

test("revoking the token just made takes its secret away: a revoked secret is no use to copy", async () => {
  const writer = profile("writer");
  serveAccount(writer, { tokens: [token(1)] });
  renderScreen(<TokensSection />, writer);
  await makeToken();
  fireEvent.click(await screen.findByRole("button", { name: "Revoke laptop trax" }));
  const dialog = screen.getByRole("alertdialog", { name: "Revoke “laptop trax”?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Revoke" }));
  await waitFor(() => expect(screen.queryByText(SECRET)).toBeNull());
  expect(screen.getByRole("button", { name: "New token" })).toBeTruthy();
});

test("revoking asks first; Cancel sends nothing, and Revoke sends one POST and dims the row", async () => {
  const writer = profile("writer");
  const server = serveAccount(writer, { tokens: [token(1, { name: "ci" })] });
  renderScreen(<TokensSection />, writer);
  fireEvent.click(await screen.findByRole("button", { name: "Revoke ci" }));
  let dialog = screen.getByRole("alertdialog", { name: "Revoke “ci”?" });
  expect(dialog.textContent).toContain("Anything using the token trk_0001… stops working at once.");
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(server.writes()).toEqual([]);

  fireEvent.click(screen.getByRole("button", { name: "Revoke ci" }));
  dialog = screen.getByRole("alertdialog", { name: "Revoke “ci”?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Revoke" }));
  await screen.findByText(/^revoked /);
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(server.writes()).toEqual([{ call: "POST /api/me/tokens/t1/revoke", body: undefined }]);
  expect(screen.queryByRole("button", { name: "Revoke ci" })).toBeNull();
  expect(screen.getByText("Revoked “ci”")).toBeTruthy();
});

test("while a revoke is pending, Cancel and Escape leave the dialog open with its state", async () => {
  const writer = profile("writer");
  const server = serveAccount(writer, { tokens: [token(1, { name: "ci" })] });
  const release = server.hold();
  renderScreen(<TokensSection />, writer);
  fireEvent.click(await screen.findByRole("button", { name: "Revoke ci" }));
  const dialog = screen.getByRole("alertdialog", { name: "Revoke “ci”?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Revoke" }));
  await within(dialog).findByText("Saving…");
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(screen.getByRole("alertdialog")).toBe(dialog);
  expect(within(dialog).getByText("Saving…")).toBeTruthy();
  release();
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  expect(screen.getByText(/^revoked /)).toBeTruthy();
});

test("Revoke is off while offline, as its confirmation is", async () => {
  vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  const writer = profile("writer");
  serveAccount(writer, { tokens: [token(1, { name: "ci" })] });
  renderScreen(<TokensSection />, writer);
  const revoke = (await screen.findByRole("button", { name: "Revoke ci" })) as HTMLButtonElement;
  expect(revoke.disabled).toBe(true);
  fireEvent.click(revoke);
  expect(screen.queryByRole("alertdialog")).toBeNull();
});

test("a failed refetch keeps the tokens on screen, beside the error and Retry", async () => {
  const writer = profile("writer");
  const server = serveAccount(writer, { tokens: [token(1, { name: "ci" })] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderScreen(<TokensSection />, writer, queryClient);
  await screen.findByRole("cell", { name: "ci" });
  server.failing.add("/api/me/tokens");
  await act(() => queryClient.refetchQueries());
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain("database unavailable");
  expect(screen.getByRole("cell", { name: "ci" })).toBeTruthy();
  server.failing.clear();
  fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(screen.getByRole("cell", { name: "ci" })).toBeTruthy();
});

test("Copy says it failed when the browser has no clipboard", async () => {
  const writer = profile("writer");
  serveAccount(writer, { tokens: [] });
  renderScreen(<TokensSection />, writer);
  const box = await makeToken();
  fireEvent.click(within(box).getByRole("button", { name: "Copy" }));
  await screen.findByText("Could not copy: the browser refused the clipboard.");
});

test("the server's refusals show next to the control: 422 for a blank label, 403 over the ceiling, 404 for a revoked token", async () => {
  const writer = profile("writer");
  const server = serveAccount(writer, { tokens: [token(1, { name: "ci" })] });
  renderScreen(<TokensSection />, writer);
  const form = await openForm();
  fireEvent.click(within(form).getByRole("button", { name: "Create token" }));
  expect((await within(form).findByRole("alert")).textContent).toBe("name: String should have at least 1 character");

  server.answers.push(() => Response.json({ detail: "requested role 'writer' exceeds ceiling 'viewer'" }, { status: 403 }));
  fireEvent.change(within(form).getByRole("textbox", { name: "Label" }), { target: { value: "x" } });
  fireEvent.click(within(form).getByRole("button", { name: "Create token" }));
  await waitFor(() =>
    expect(within(form).getByRole("alert").textContent).toBe(
      "Your role cannot make this change. requested role 'writer' exceeds ceiling 'viewer'",
    ),
  );

  server.answers.push(() => Response.json({ detail: "token not found" }, { status: 404 }));
  fireEvent.click(screen.getByRole("button", { name: "Revoke ci" }));
  const dialog = screen.getByRole("alertdialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Revoke" }));
  expect((await within(dialog).findByRole("alert")).textContent).toBe("token not found");
  expect(screen.getByRole("alertdialog")).toBe(dialog);
});

test("a create with no answer is never sent again: the list shows whether the token was made", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const writer = profile("writer");
  const server = serveAccount(writer, { tokens: [] });
  // The server makes the token, but its answer never comes; the request aborts at the write timeout.
  server.answers.push((request) => {
    server.tokens = [token(1, { name: "ci" })];
    return new Promise((_, reject) => request.signal.addEventListener("abort", () => reject(request.signal.reason)));
  });
  renderScreen(<TokensSection />, writer);
  const form = await openForm();
  fireEvent.change(within(form).getByRole("textbox", { name: "Label" }), { target: { value: "ci" } });
  fireEvent.click(within(form).getByRole("button", { name: "Create token" }));
  await act(() => vi.advanceTimersByTimeAsync(30_000));
  const alert = await within(form).findByRole("alert");
  expect(alert.textContent).toBe(
    "The server did not confirm the token, so it may have been made anyway: the list below shows whether it was. Its secret cannot be shown again, so revoke it there if it was.",
  );
  expect(within(alert).queryByRole("button")).toBeNull();
  await screen.findByRole("cell", { name: "ci" });
  // The waits an inquiry write would retry after: nothing is resent.
  await act(() => vi.advanceTimersByTimeAsync(15_000));
  expect(server.writes()).toHaveLength(1);
});

test("a create the server failed offers no Retry, keeps the form as it was, and the next Create is a new one", async () => {
  const writer = profile("writer");
  const server = serveAccount(writer, { tokens: [] });
  server.answers.push(() => Response.json({ detail: "database unavailable" }, { status: 503 }));
  renderScreen(<TokensSection />, writer);
  const form = await openForm();
  const label = within(form).getByRole("textbox", { name: "Label" }) as HTMLInputElement;
  fireEvent.change(label, { target: { value: "ci" } });
  fireEvent.click(within(form).getByRole("button", { name: "Create token" }));
  const alert = await within(form).findByRole("alert");
  expect(alert.textContent).toContain("may have been made anyway");
  expect(within(alert).queryByRole("button")).toBeNull();
  expect(label.value).toBe("ci");
  expect(server.writes()).toHaveLength(1);

  fireEvent.change(label, { target: { value: "ci 2" } });
  fireEvent.click(within(form).getByRole("button", { name: "Create token" }));
  await screen.findByText("trk_new1SECRET-t1");
  expect(server.writes().map(({ body }) => body)).toEqual([
    { name: "ci", role: "writer" },
    { name: "ci 2", role: "writer" },
  ]);
});
