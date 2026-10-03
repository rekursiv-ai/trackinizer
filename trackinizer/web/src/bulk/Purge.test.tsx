import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { DetailRow } from "../api/detail";
import { stubFetch } from "../api/testing";
import { detail, renderDetail, row } from "../detail/testing";
import { stubLayout } from "../editors/testing";

beforeEach(() => {
  history.replaceState(null, "", "#/ref/Issue/7");
  stubLayout();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/**
 * A server holding one Issue, as trackinizer answers: owner writes by
 * compare-and-set, and a purge refused (409) while the row has an owner, after
 * which the row is gone (404).
 */
function serve(self: DetailRow) {
  const server = {
    self,
    purged: false,
    writes: [] as string[],
    bodies: [] as unknown[],
    /** Answers the owner write once it resolves. */
    ownerHeld: null as Promise<void> | null,
  };
  stubFetch(async (request) => {
    const { pathname } = new URL(request.url);
    if (pathname.endsWith("/owner") && server.ownerHeld) await server.ownerHeld;
    if (request.method === "GET") {
      if (server.purged) return Response.json({ detail: "inquiry not found" }, { status: 404 });
      if (pathname === `/api/web/get/${self.id}`) return Response.json(detail(server.self));
      return Response.json({ confidence: 0.5 });
    }
    const body = (await request.clone().json()) as { value?: unknown; expected?: unknown };
    server.writes.push(`${request.method} ${pathname.replace(self.id, "{id}")}`);
    server.bodies.push(body);
    if (pathname.endsWith("/owner")) {
      if (body.expected !== server.self.owner) return Response.json({ detail: "owner transition rejected" }, { status: 409 });
      server.self = { ...server.self, owner: body.value ?? null };
      return Response.json({ id: self.id, change_id: "c1" });
    }
    if (server.self.owner) {
      return Response.json({ detail: `inquiry is owned by '${server.self.owner}'; release its owner before purge` }, { status: 409 });
    }
    server.purged = true;
    return Response.json({ id: self.id, change_id: "c2" });
  });
  return server;
}

async function openPurge() {
  renderDetail({ id: row("Issue", 7).id });
  await screen.findByRole("heading", { level: 1 });
  fireEvent.click(screen.getByRole("button", { name: "Issue#7 actions" }));
  fireEvent.click(screen.getByRole("option", { name: "Purge…" }));
  return screen.findByRole("dialog", { name: "Purge Issue#7" });
}

test("an owned row: the dialog offers to clear the owner, needs a reason, then clears it by compare-and-set and purges", { tags: ["manual"] }, async () => {
  const server = serve(row("Issue", 7, { owner: "bob" }));
  const dialog = await openPurge();
  expect(dialog.textContent).toContain("Issue#7 is owned by bob.");
  const confirm = within(dialog).getByRole("button", { name: /^Clear owner and purge/ });
  fireEvent.click(confirm);
  expect(within(dialog).getByRole("alert").textContent).toBe("Add a reason first.");
  expect(server.writes).toEqual([]);

  fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), { target: { value: "  A duplicate.  " } });
  fireEvent.click(confirm);
  await screen.findByRole("heading", { name: "Deleted or purged" });
  expect(server.writes).toEqual(["PUT /api/inquiries/{id}/owner", "DELETE /api/inquiries/{id}"]);
  expect(server.bodies).toEqual([
    { value: null, mode: "cas", expected: "bob", reason: "A duplicate." },
    { reason: "A duplicate." },
  ]);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByText("Purged Issue#7")).toBeTruthy();
});

test("an unowned row is purged with one DELETE, after one confirm", async () => {
  const server = serve(row("Issue", 7));
  const dialog = await openPurge();
  expect(dialog.textContent).not.toContain("owned by");
  fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), { target: { value: "Test row" } });
  fireEvent.keyDown(within(dialog).getByRole("textbox", { name: "Reason" }), { key: "Enter", metaKey: true });
  await screen.findByRole("heading", { name: "Deleted or purged" });
  expect(server.writes).toEqual(["DELETE /api/inquiries/{id}"]);
  expect(server.bodies).toEqual([{ reason: "Test row" }]);
});

test("a row someone took after the detail loaded: the refusal shows, and the dialog then offers to clear the new owner", async () => {
  const server = serve(row("Issue", 7));
  const dialog = await openPurge();
  server.self = { ...server.self, owner: "carol" };
  fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), { target: { value: "Stale" } });
  fireEvent.click(within(dialog).getByRole("button", { name: /^Purge permanently/ }));
  await waitFor(() =>
    expect(within(dialog).getByRole("alert").textContent).toBe("inquiry is owned by 'carol'; release its owner before purge"),
  );
  await within(dialog).findByRole("button", { name: /^Clear owner and purge/ });
  expect(dialog.textContent).toContain("Issue#7 is owned by carol.");
  expect(server.purged).toBe(false);
});

test("a dialog closed while the owner is cleared purges nothing once that lands (RV-04)", async () => {
  const server = serve(row("Issue", 7, { owner: "bob" }));
  let release = () => {};
  server.ownerHeld = new Promise((resolve) => (release = resolve));
  const dialog = await openPurge();
  fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), { target: { value: "A duplicate." } });
  fireEvent.click(within(dialog).getByRole("button", { name: /^Clear owner and purge/ }));
  await within(dialog).findByText("Saving…");
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  release();
  // A write the server receives changes nothing on screen, so only the interval checks again.
  await waitFor(() => expect(server.self.owner).toBeNull(), { interval: 1 });
  // Turns enough for a purge to follow, as it did before (RV-04).
  for (let turn = 0; turn < 5; turn++) await act(() => new Promise((resolve) => setTimeout(resolve)));
  expect(server.writes).toEqual(["PUT /api/inquiries/{id}/owner"]);
  expect(server.purged).toBe(false);
});

test("⌘↵ pressed while an input method composes does not purge (REV-D4-01's class)", async () => {
  const server = serve(row("Issue", 7));
  const dialog = await openPurge();
  const reason = within(dialog).getByRole("textbox", { name: "Reason" });
  fireEvent.change(reason, { target: { value: "語" } });
  fireEvent.keyDown(reason, { key: "Enter", metaKey: true, isComposing: true });
  await act(() => new Promise((resolve) => setTimeout(resolve)));
  expect(server.writes).toEqual([]);
});
