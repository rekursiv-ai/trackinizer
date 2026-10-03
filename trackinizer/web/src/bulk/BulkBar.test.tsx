import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { InquiryRow } from "../api/inquiries";
import type { Profile } from "../api/me";
import { type Sent, stubFetch } from "../api/testing";
import { MetaContext, ProfileContext } from "../app/boot";
import { createQueryClient } from "../app/queryClient";
import { CommandRegistry, CommandRegistryContext, Shortcuts } from "../commands/registry";
import { META, PROFILE, uuid } from "../detail/testing";
import { stubLayout } from "../editors/testing";
import { ListView } from "../lists";
import { RouterProvider } from "../router/router";
import { ToastProvider } from "../ui/toast";

beforeEach(() => {
  history.replaceState(null, "", "#/list/Issue");
  sessionStorage.clear();
  stubLayout();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
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

/**
 * A server holding `rows`: lists them newest first under the list's `is` and
 * `ne` filters, and applies status writes by compare-and-set, changing nothing
 * for a row already so. `answers[seq]` take that row's next writes over.
 */
function serve(rows: InquiryRow[]) {
  const server = { rows, sent: [] as Sent[], answers: {} as { [seq: number]: (() => Response)[] } };
  server.sent = stubFetch(async (request) => {
    const { pathname, searchParams } = new URL(request.url);
    if (request.method === "GET" && pathname !== "/api/inquiries") {
      return Response.json(server.rows.find((row) => pathname.endsWith(row.id)));
    }
    if (request.method === "GET") {
      const filters = searchParams.getAll("filter").map((raw) => JSON.parse(raw) as { field: string; op: string; value: string });
      const match = (row: InquiryRow) =>
        filters.every(({ field, op, value }) => (row[field as keyof InquiryRow] === value) === (op === "is"));
      return Response.json(server.rows.filter(match).toSorted((a, b) => b.seq - a.seq));
    }
    const at = server.rows.findIndex((row) => pathname.includes(row.id));
    const scripted = server.answers[server.rows[at]!.seq]?.shift();
    if (scripted) return scripted();
    const body = (await request.clone().json()) as { value: string; expected: string };
    if (body.expected !== server.rows[at]!.status) return Response.json({ detail: "status transition rejected" }, { status: 409 });
    if (body.value === server.rows[at]!.status) return Response.json({ id: server.rows[at]!.id, change_id: null });
    server.rows[at] = { ...server.rows[at]!, status: body.value };
    return Response.json({ id: server.rows[at]!.id, change_id: "c" });
  });
  return server;
}

/** Render the Issue list; returns its query cache. */
function show(profile: Profile = PROFILE) {
  const queryClient = createQueryClient(() => {});
  render(
    <QueryClientProvider client={queryClient}>
      <CommandRegistryContext value={new CommandRegistry()}>
        <ToastProvider>
          <Shortcuts />
          <MetaContext value={META}>
            <ProfileContext value={profile}>
              <RouterProvider kinds={META.kinds}>
                <ListView kind="Issue" />
              </RouterProvider>
            </ProfileContext>
          </MetaContext>
        </ToastProvider>
      </CommandRegistryContext>
    </QueryClientProvider>,
  );
  return queryClient;
}

// By its label: a query by role would compute the name of every button on the
// page, some 10 ms a call, and the label is all these tests look the check up by.
const check = (seq: number) => document.querySelector<HTMLElement>(`button[aria-label="Select Issue#${seq}"]`)!;
const bar = () => screen.queryByRole("group", { name: "Bulk actions" });
const selected = () => [...document.querySelectorAll(".row-line.is-selected .row-title")].map((title) => title.textContent);

test("rows are selected by their check, a shift-click over a range, and x; Escape clears them", async () => {
  serve([1, 2, 3, 4].map((seq) => issue(seq)));
  show();
  await screen.findByText("Issue 4");
  fireEvent.click(check(4));
  expect(within(bar()!).getByText(/selected/).textContent).toBe("1 selected");
  // Shown newest first: 4, 3, 2, 1. A shift-click on a row selects too, and opens nothing.
  fireEvent.click(screen.getByText("Issue 2"), { shiftKey: true });
  expect(selected()).toEqual(["Issue 4", "Issue 3", "Issue 2"]);
  expect(location.hash).toBe("#/list/Issue");
  expect(check(3).getAttribute("aria-pressed")).toBe("true");

  // The focused row is the first; x takes it out, and puts it back.
  await userEvent.keyboard("x");
  expect(selected()).toEqual(["Issue 3", "Issue 2"]);
  await userEvent.keyboard("x");
  expect(selected()).toHaveLength(3);
  await userEvent.keyboard("{Escape}");
  expect(selected()).toEqual([]);
  expect(bar()).toBeNull();
});

test("a status change sends one compare-and-set PUT per row; the report names the row that failed, and Retry sends it alone", { tags: ["manual"] }, async () => {
  const server = serve([issue(1), issue(2), issue(3, { status: "complete" })]);
  const unavailable = () => Response.json({ detail: "database unavailable" }, { status: 503 });
  server.answers[2] = [unavailable, unavailable, unavailable, unavailable];
  show();
  await screen.findByText("Issue 2");
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  await screen.findByText("Issue 3");
  for (const seq of [1, 2, 3]) fireEvent.click(check(seq));
  // After rendering: Testing Library's polling waits on the real `setTimeout`.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(within(bar()!).getByRole("button", { name: "Status" }));
  fireEvent.click(screen.getByRole("option", { name: "Complete" }));
  expect(within(bar()!).getByRole("status").textContent).toBe("Saving… 0 of 3");
  await act(() => vi.runAllTimersAsync());

  const report = screen.getByRole("region", { name: "Status set to Complete: results" });
  expect(within(report).getByRole("alert").textContent).toBe("Status set to Complete: 1 of 3 not saved.");
  // The rows that did not land first, then the others in the list's order.
  expect([...report.querySelectorAll("li")].map((item) => item.textContent)).toEqual([
    "Issue#2Issue 2Not saved: the server failed (database unavailable).",
    "Issue#3Issue 3Already so",
    "Issue#1Issue 1Saved",
  ]);
  const writes = () => server.sent.filter((request) => request.method === "PUT");
  // Issue#3 was complete already, which the server says; Issue#2 was tried four times with one key.
  expect(writes().map((request) => request.path.split("/")[3]).toSorted()).toEqual([uuid(1), uuid(2), uuid(2), uuid(2), uuid(2), uuid(3)]);
  expect(writes().filter((request) => !request.path.includes(uuid(3))).map((request) => request.body)).toEqual(
    Array.from({ length: 5 }, () => ({ value: "complete", mode: "cas", expected: "active" })),
  );
  expect(new Set(writes().map((request) => request.headers["idempotency-key"])).size).toBe(3);

  // Real timers again: running every timer would also close the toast (5 s).
  vi.useRealTimers();
  fireEvent.click(within(report).getByRole("button", { name: "Retry 1 failed" }));
  // The toast sums up the whole edit, rows saved on the first run included.
  await screen.findByText("Status set to Complete on 2 inquiries; 1 was already so.");
  expect(writes()).toHaveLength(7);
  expect(writes()[6]).toEqual(writes().filter((request) => request.path.includes(uuid(2)))[0]);
  expect(screen.queryByRole("region", { name: /results/ })).toBeNull();
  expect(server.rows.map((row) => row.status)).toEqual(["complete", "complete", "complete"]);
});

test("a row someone changed since the list loaded is a conflict, and Retry saves over the value now stored", async () => {
  const server = serve([issue(1), issue(2)]);
  show();
  await screen.findByText("Issue 2");
  fireEvent.click(check(2));
  fireEvent.click(check(1), { shiftKey: true });
  server.rows[0] = { ...server.rows[0]!, status: "abandoned" };
  fireEvent.click(within(bar()!).getByRole("button", { name: "Status" }));
  fireEvent.click(screen.getByRole("option", { name: "Complete" }));
  const report = await screen.findByRole("region", { name: /results/ });
  expect(report.querySelector("li")!.textContent).toBe(
    "Issue#1Issue 1Changed to Abandoned since the list loaded it. Retry saves yours over it.",
  );
  fireEvent.click(within(report).getByRole("button", { name: "Retry 1 failed" }));
  await waitFor(() => expect(screen.queryByRole("region", { name: /results/ })).toBeNull());
  const retried = server.sent.filter((request) => request.method === "PUT").at(-1)!;
  expect(retried.body).toEqual({ value: "complete", mode: "cas", expected: "abandoned" });
  expect(server.rows.map((row) => row.status)).toEqual(["complete", "complete"]);
});

test("abandoning asks for a reason once, for every row", async () => {
  const server = serve([issue(1), issue(2)]);
  show();
  await screen.findByText("Issue 2");
  fireEvent.click(check(1));
  fireEvent.click(check(2));
  fireEvent.click(within(bar()!).getByRole("button", { name: "Status" }));
  fireEvent.click(screen.getByRole("option", { name: "Abandoned" }));
  const dialog = await screen.findByRole("dialog", { name: "Abandon 2 inquiries" });
  fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), { target: { value: "Out of scope" } });
  fireEvent.click(within(dialog).getByRole("button", { name: /^Abandon/ }));
  await screen.findByText("Status set to Abandoned on 2 inquiries.");
  expect(server.sent.filter((request) => request.method === "PUT").map((request) => request.body)).toEqual([
    { value: "abandoned", mode: "cas", expected: "active", reason: "Out of scope" },
    { value: "abandoned", mode: "cas", expected: "active", reason: "Out of scope" },
  ]);
});

test("rows of different priorities tick none in the Priority menu, and rows of one tick it", async () => {
  serve([issue(1, { priority: 0 }), issue(2, { priority: 20 }), issue(3, { priority: 20 })]);
  show();
  await screen.findByText("Issue 3");
  const ticked = () => screen.getAllByRole("option").filter((option) => option.getAttribute("aria-selected") === "true");
  fireEvent.click(check(1));
  fireEvent.click(check(2));
  fireEvent.click(within(bar()!).getByRole("button", { name: "Priority" }));
  expect(ticked()).toEqual([]);
  await userEvent.keyboard("{Escape}");
  fireEvent.click(check(1));
  fireEvent.click(check(3));
  fireEvent.click(within(bar()!).getByRole("button", { name: "Priority" }));
  expect(ticked().map((option) => option.textContent)).toEqual(["P2 Medium"]);
});

test("a viewer gets no checks and no bar", async () => {
  serve([issue(1), issue(2)]);
  show({ ...PROFILE, role: "viewer" });
  await screen.findByText("Issue 2");
  expect(screen.queryByRole("button", { name: /^Select/ })).toBeNull();
  await userEvent.keyboard("x");
  expect(bar()).toBeNull();
});

test("a reason asked for applies to the rows as they were when the status was picked (BR-14)", { tags: ["manual"] }, async () => {
  const server = serve([issue(1), issue(2)]);
  const queryClient = show();
  await screen.findByText("Issue 2");
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  fireEvent.click(await screen.findByRole("button", { name: "Select Issue#1" }));
  fireEvent.click(check(2));
  fireEvent.click(within(bar()!).getByRole("button", { name: "Status" }));
  fireEvent.click(screen.getByRole("option", { name: "Abandoned" }));
  const dialog = await screen.findByRole("dialog", { name: "Abandon 2 inquiries" });
  // Someone completes Issue#1, and the list shows it before the reason is given.
  server.rows[0] = { ...server.rows[0]!, status: "complete" };
  await act(() => queryClient.refetchQueries({ queryKey: ["inquiries"] }));
  await waitFor(() => expect(document.querySelector(`[data-row="${uuid(1)}"] svg[aria-label="Complete"]`)).not.toBeNull());
  fireEvent.click(within(dialog).getByRole("button", { name: /^Abandon/ }));
  const report = await screen.findByRole("region", { name: /results/ });
  expect(report.querySelector("li")!.textContent).toBe("Issue#1Issue 1Changed to Complete since the list loaded it. Retry saves yours over it.");
  expect(server.sent.filter((request) => request.method === "PUT").map((request) => request.body)).toEqual([
    { value: "abandoned", mode: "cas", expected: "active" },
    { value: "abandoned", mode: "cas", expected: "active" },
  ]);
});

test("a report of rows not saved stays until retried or closed: a new change waits for it (F5-02)", async () => {
  const server = serve([issue(1), issue(2)]);
  const unavailable = () => Response.json({ detail: "database unavailable" }, { status: 503 });
  server.answers[1] = [unavailable, unavailable, unavailable, unavailable];
  show();
  await screen.findByText("Issue 2");
  fireEvent.click(check(1));
  fireEvent.click(check(2));
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(within(bar()!).getByRole("button", { name: "Status" }));
  fireEvent.click(screen.getByRole("option", { name: "Complete" }));
  await act(() => vi.runAllTimersAsync());
  vi.useRealTimers();
  const report = screen.getByRole("region", { name: "Status set to Complete: results" });
  const sent = server.sent.length;

  const owner = within(bar()!).getByRole("button", { name: "Owner" });
  expect(owner.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(owner);
  expect(screen.queryByRole("combobox")).toBeNull();
  expect(server.sent).toHaveLength(sent);
  expect(within(report).getByRole("button", { name: "Retry 1 failed" })).toBeTruthy();

  fireEvent.click(within(report).getByRole("button", { name: "Close" }));
  expect(owner.getAttribute("aria-disabled")).toBeNull();
});

test("clearing the selection forgets where a shift-click range starts (SEL-ANCHOR-01)", async () => {
  serve([1, 2, 3, 4].map((seq) => issue(seq)));
  show();
  await screen.findByText("Issue 4");
  fireEvent.click(check(4));
  fireEvent.click(within(bar()!).getByRole("button", { name: "Clear selection" }));
  fireEvent.click(check(2), { shiftKey: true });
  expect(selected()).toEqual(["Issue 2"]);
});
