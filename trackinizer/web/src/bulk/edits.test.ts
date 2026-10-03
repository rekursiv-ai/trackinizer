import { afterEach, expect, test, vi } from "vitest";
import type { InquiryRow } from "../api/inquiries";
import { type Sent, stubFetch } from "../api/testing";
import { row as detailRow, uuid } from "../detail/testing";
import { clearOwnerEdit, purgeEdit, setEach, toggleLabel } from "./edits";

afterEach(() => {
  vi.unstubAllGlobals();
});

function issue(seq: number, fields: Partial<InquiryRow> = {}): InquiryRow {
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
    ...fields,
  };
}

/** Send each row's edit, and return what the server received, without the key. */
async function sent(edits: readonly { request: { send: () => Promise<unknown> } }[]): Promise<Omit<Sent, "headers" | "query">[]> {
  const requests = stubFetch(() => Response.json({ id: "x", change_id: "c1" }));
  for (const edit of edits) await edit.request.send();
  for (const request of requests) expect(request.headers["idempotency-key"]).toMatch(/^[0-9a-f-]{36}$/);
  expect(new Set(requests.map((request) => request.headers["idempotency-key"])).size).toBe(requests.length);
  return requests.map(({ method, path, body }) => ({ method, path, body }));
}

test("status goes to every row by compare-and-set with the value each row showed, one already there too (R4-F04)", async () => {
  const rows = [issue(1), issue(2, { status: "abandoned" }), issue(3, { status: "complete" })];
  const change = setEach("status", "complete", { label: "Status", show: String, reason: "shipped" });
  expect(change.title).toBe("Status set to complete");
  expect(await sent(rows.map((row) => change.edit(row)))).toEqual([
    { method: "PUT", path: `/api/inquiries/${uuid(1)}/status`, body: { value: "complete", mode: "cas", expected: "active", reason: "shipped" } },
    { method: "PUT", path: `/api/inquiries/${uuid(2)}/status`, body: { value: "complete", mode: "cas", expected: "abandoned", reason: "shipped" } },
    { method: "PUT", path: `/api/inquiries/${uuid(3)}/status`, body: { value: "complete", mode: "cas", expected: "complete", reason: "shipped" } },
  ]);
});

test("owner is compare-and-set too, and No owner writes null the same way", async () => {
  const rows = [issue(1), issue(2, { owner: "bob" })];
  const assign = setEach("owner", "ada@example.com", { label: "Owner", show: String });
  const clear = setEach("owner", null, { label: "Owner", show: String });
  expect(clear.title).toBe("Cleared owner");
  expect(await sent([...rows.map((row) => assign.edit(row)), clear.edit(rows[1]!)])).toEqual([
    { method: "PUT", path: `/api/inquiries/${uuid(1)}/owner`, body: { value: "ada@example.com", mode: "cas", expected: null } },
    { method: "PUT", path: `/api/inquiries/${uuid(2)}/owner`, body: { value: "ada@example.com", mode: "cas", expected: "bob" } },
    { method: "PUT", path: `/api/inquiries/${uuid(2)}/owner`, body: { value: null, mode: "cas", expected: "bob" } },
  ]);
});

test("priority is a plain PUT on the Issue route, and No priority a DELETE with {}", async () => {
  const rows = [issue(1, { priority: 20 }), issue(2, { priority: 10 })];
  const high = setEach("priority", 10, { label: "Priority", show: String });
  const none = setEach("priority", null, { label: "Priority", show: String });
  expect(await sent([...rows.map((row) => high.edit(row)), none.edit(rows[1]!)])).toEqual([
    { method: "PUT", path: `/api/issue/${uuid(1)}/priority`, body: { value: 10 } },
    { method: "PUT", path: `/api/issue/${uuid(2)}/priority`, body: { value: 10 } },
    { method: "DELETE", path: `/api/issue/${uuid(2)}/priority`, body: {} },
  ]);
  expect(high.edit(rows[0]!).guard).toMatchObject({ type: "check", base: 20, mine: 10 });
});

test("a label goes on every row, one PATCH each; a row that has it already changes nothing (R4-F04)", async () => {
  const rows = [issue(1, { labels: ["keep"] }), issue(2, { labels: ["new"] }), issue(3)];
  const change = toggleLabel("new", rows);
  expect(change.title).toBe("Added label new");
  expect(await sent(rows.map((row) => change.edit(row)))).toEqual(
    [1, 2, 3].map((seq) => ({ method: "PATCH", path: `/api/inquiries/${uuid(seq)}/labels`, body: { op: "add", value: "new" } })),
  );
});

test("a label every row has comes off each with one PATCH, a row's last label too (R4-F02)", async () => {
  const rows = [issue(1, { labels: ["old", "keep"] }), issue(2, { labels: ["old"] })];
  const change = toggleLabel("old", rows);
  expect(change.title).toBe("Removed label old");
  expect(await sent(rows.map((row) => change.edit(row)))).toEqual([
    { method: "PATCH", path: `/api/inquiries/${uuid(1)}/labels`, body: { op: "sub", value: "old" } },
    { method: "PATCH", path: `/api/inquiries/${uuid(2)}/labels`, body: { op: "sub", value: "old" } },
  ]);
});

test("purging an owned row clears its owner by compare-and-set, then purges, both with the reason; nothing sends actor", async () => {
  const owned = detailRow("Issue", 7, { owner: "bob" });
  const clear = clearOwnerEdit(owned, "duplicate of Issue#6");
  expect(clear.done).toBeUndefined();
  expect(clear.undo).toBeUndefined();
  const purge = purgeEdit(owned, "duplicate of Issue#6");
  expect(purge.touches).toEqual([owned.id]);
  expect(purge.done).toBe("Purged Issue#7");
  expect(await sent([clear, purge])).toEqual([
    { method: "PUT", path: `/api/inquiries/${uuid(7)}/owner`, body: { value: null, mode: "cas", expected: "bob", reason: "duplicate of Issue#6" } },
    { method: "DELETE", path: `/api/inquiries/${uuid(7)}`, body: { reason: "duplicate of Issue#6" } },
  ]);
});
