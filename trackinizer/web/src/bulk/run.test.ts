import { afterEach, expect, test, vi } from "vitest";
import { ApiError } from "../api/client";
import type { FieldWrite, InquiryRow } from "../api/inquiries";
import { stubFetch } from "../api/testing";
import { row as detailRow, uuid } from "../detail/testing";
import type { Edit } from "../writes/edits";
import { sendWithRetries, type WriteRequest } from "../writes/requests";
import { setEach } from "./edits";
import { CONCURRENT, type Entry, forRetry, type Io, retryCount, sendPending, settle, summary } from "./run";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function issue(seq: number, status = "active", priority: number | null = null): InquiryRow {
  return {
    id: uuid(seq),
    kind: "Issue",
    seq,
    title: `Issue ${seq}`,
    status,
    owner: null,
    labels: null,
    priority,
    marginal_cost: { agent_usd: 0, resource_usd: 0 },
    created: "2026-09-20T00:00:00+00:00",
    modified: "2026-09-20T00:00:00+00:00",
  };
}

const COMPLETE = setEach("status", "complete", { label: "Status", show: String });
const HIGH = setEach("priority", 10, { label: "Priority", show: String });

/**
 * An `Io` whose sends answer with `answer(request)`; a guard's read of row 1 finds
 * it with `stored` fields, as the server has it now.
 */
function io(answer: (request: WriteRequest<FieldWrite>) => Promise<FieldWrite>, stored: { [field: string]: unknown } = {}): Io {
  stubFetch(() => Response.json(detailRow("Issue", 1, stored)));
  return { send: answer };
}

/**
 * Settle `edit` with the write layer's retries against a row that reads as
 * Issue 1 with priority 20: its first write reaches the server, which then
 * holds `after`, and the answer is lost; a second write lands.
 */
async function settleLost(edit: Edit<FieldWrite>, after: { [field: string]: unknown }) {
  vi.useFakeTimers();
  let fields: { [field: string]: unknown } = { priority: 20 };
  const sent = stubFetch(() => {
    if (sent.at(-1)!.method === "GET") return Response.json(detailRow("Issue", 1, fields));
    if (sent.filter((request) => request.method !== "GET").length > 1) return Response.json({ id: "x", change_id: "c" });
    fields = after;
    return Promise.reject(new TypeError("Failed to fetch"));
  });
  const outcome = settle(edit, { send: sendWithRetries });
  await vi.runAllTimersAsync();
  return { outcome: await outcome, writes: sent.filter((request) => request.method !== "GET") };
}

const ok = async (): Promise<FieldWrite> => ({ id: "x", change_id: "c" });
const refuse = (status: number, detail: string) => async (): Promise<FieldWrite> => {
  throw new ApiError(status, detail);
};

test("a landed write is done; no answer or a 5xx is a failure, and any other refusal the server's message", async () => {
  const edit = COMPLETE.edit(issue(1))!;
  expect(await settle(edit, io(ok))).toEqual({ status: "done" });
  expect(await settle(edit, io(refuse(503, "database unavailable")))).toEqual({
    status: "failed",
    message: "Not saved: the server failed (database unavailable).",
  });
  expect(await settle(edit, io(refuse(0, "No response within 30 s.")))).toEqual({
    status: "failed",
    message: "Not saved. No response within 30 s.",
  });
  expect(await settle(edit, io(refuse(422, "value: Input should be a valid string")))).toEqual({
    status: "rejected",
    message: "value: Input should be a valid string",
  });
});

test("a compare-and-set 409 reads the row: moved is a conflict, moved to mine unchanged, unmoved the server's refusal", async () => {
  const edit = COMPLETE.edit(issue(1))!;
  const conflict = refuse(409, "status transition rejected");
  expect(await settle(edit, io(conflict, { status: "abandoned" }))).toEqual({
    status: "conflict",
    theirs: "abandoned",
    message: "Changed to abandoned since the list loaded it. Retry saves yours over it.",
  });
  expect(await settle(edit, io(conflict, { status: "complete" }))).toEqual({ status: "unchanged" });
  expect(await settle(edit, io(conflict, { status: "active" }))).toEqual({
    status: "rejected",
    message: "status transition rejected",
  });
});

test("a field with no compare-and-set is checked first: one someone changed is a conflict, and nothing is sent (CR-W01)", async () => {
  const edit = HIGH.edit(issue(1, "active", 20))!;
  const sent: WriteRequest<FieldWrite>[] = [];
  const record = async (request: WriteRequest<FieldWrite>) => {
    sent.push(request);
    return { id: "x", change_id: "c" };
  };
  expect(await settle(edit, io(record, { priority: 30 }))).toEqual({
    status: "conflict",
    theirs: 30,
    message: "Changed to 30 since the list loaded it. Retry saves yours over it.",
  });
  expect(await settle(edit, io(record, { priority: 10 }))).toEqual({ status: "unchanged" });
  expect(sent).toEqual([]);
  expect(await settle(edit, io(record, { priority: 20 }))).toEqual({ status: "done" });
  expect(sent).toEqual([edit.request]);
  // Retry saves mine over the value now stored, checked against it in turn.
  const [retried] = forRetry([{ row: issue(1), edit, outcome: { status: "conflict", message: "Changed.", theirs: 30 } }]);
  expect(retried!.edit.guard).toMatchObject({ type: "check", base: 30, mine: 10 });
});

test("a row the list showed at the value is still sent, and the server says whether it changed (R4-F04)", async () => {
  const edit = COMPLETE.edit(issue(1, "complete"));
  expect(edit.guard).toMatchObject({ type: "cas", base: "complete", mine: "complete" });
  expect(await settle(edit, io(async () => ({ id: "x", change_id: null })))).toEqual({ status: "unchanged" });
  // Someone made it active since the list loaded it: that is a conflict, not "already so".
  expect(await settle(edit, io(refuse(409, "status transition rejected"), { status: "active" }))).toEqual({
    status: "conflict",
    theirs: "active",
    message: "Changed to active since the list loaded it. Retry saves yours over it.",
  });
});

test("a row whose answer was lost after it landed is read back, done, and not sent again", async () => {
  const { outcome, writes } = await settleLost(HIGH.edit(issue(1, "active", 20)), { priority: 10 });
  expect(outcome).toEqual({ status: "done" });
  expect(writes).toHaveLength(1);
});

test("a row whose write never reached the server is read back and sent again with the same key", async () => {
  const { outcome, writes } = await settleLost(HIGH.edit(issue(1, "active", 20)), { priority: 20 });
  expect(outcome).toEqual({ status: "done" });
  expect(writes).toHaveLength(2);
  expect(writes[1]).toEqual(writes[0]);
});

test("a row whose answer was lost after someone else changed it is a conflict, and is not sent again", async () => {
  const { outcome, writes } = await settleLost(HIGH.edit(issue(1, "active", 20)), { priority: 30 });
  expect(outcome).toEqual({
    status: "conflict",
    theirs: 30,
    message: "Changed to 30 since the list loaded it. Retry saves yours over it.",
  });
  expect(writes).toHaveLength(1);
});

test("rows are sent at most CONCURRENT at a time, and each reports as it settles", async () => {
  const rows = Array.from({ length: CONCURRENT + 2 }, (_, n) => issue(n + 1));
  const entries: Entry[] = rows.map((row) => ({ row, edit: COMPLETE.edit(row), outcome: { status: "pending" } }));
  let inFlight = 0;
  let most = 0;
  const settled: number[] = [];
  await sendPending(
    entries,
    io(async () => {
      most = Math.max(most, ++inFlight);
      await new Promise((resolve) => setTimeout(resolve, 0));
      inFlight--;
      return { id: "x", change_id: "c" };
    }),
    (index, entry) => {
      settled.push(index);
      expect(entry.outcome).toEqual({ status: "done" });
    },
  );
  expect(most).toBe(CONCURRENT);
  expect(settled.toSorted()).toEqual(rows.map((_, index) => index));
});

test("a retry sends only failed rows, with the same request, and conflicts as a fresh edit expecting theirs", async () => {
  const rows = [issue(1), issue(2), issue(3), issue(4)];
  const edits = rows.map((row) => COMPLETE.edit(row));
  const entries: Entry[] = [
    { row: rows[0]!, edit: edits[0]!, outcome: { status: "done" } },
    { row: rows[1]!, edit: edits[1]!, outcome: { status: "failed", message: "Not saved." } },
    { row: rows[2]!, edit: edits[2]!, outcome: { status: "conflict", message: "Changed.", theirs: "abandoned" } },
    { row: rows[3]!, edit: edits[3]!, outcome: { status: "rejected", message: "Gone." } },
  ];
  expect(retryCount(entries)).toBe(2);
  const retried = forRetry(entries);
  expect(retried.map((entry) => entry.outcome.status)).toEqual(["done", "pending", "pending", "rejected"]);
  expect(retried[1]!.edit.request).toBe(edits[1]!.request);
  expect(retried[2]!.edit.request).not.toBe(edits[2]!.request);
  expect(retried[2]!.edit.guard).toMatchObject({ type: "cas", base: "abandoned", mine: "complete" });

  const sent: WriteRequest<FieldWrite>[] = [];
  const record = async (request: WriteRequest<FieldWrite>) => {
    sent.push(request);
    return { id: "x", change_id: "c" };
  };
  await sendPending(retried, io(record), () => {});
  expect(sent).toEqual([edits[1]!.request, retried[2]!.edit.request]);
});

test("the toast counts rows saved and rows already so", () => {
  const entry = (status: "done" | "unchanged"): Entry => ({ row: issue(1), edit: COMPLETE.edit(issue(1)), outcome: { status } });
  expect(summary("Status set to complete", [entry("done"), entry("done"), entry("unchanged")])).toBe(
    "Status set to complete on 2 inquiries; 1 was already so.",
  );
  expect(summary("Added label x", [entry("done")])).toBe("Added label x on 1 inquiry.");
  expect(summary("Cleared owner", [entry("unchanged"), entry("unchanged")])).toBe("Cleared owner: all 2 were already so.");
});
