import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import type { Change, DetailRow, PeersByEdge } from "../api/detail";
import { editableFields } from "../api/fields";
import { AGREED, type Sent, stubFetch } from "../api/testing";
import { bootQueries } from "../app/boot";
import { createQueryClient } from "../app/queryClient";
import { stubClipboard } from "../debug/testing";
import { detailQueries } from "../detail/queries";
import { change, detail, peer, row, uuid } from "../detail/testing";
import { LiveHub } from "../live/hub";
import { LiveContext } from "../live/index";
import { ToastProvider } from "../ui/toast";
import { type Edit, edgeAnnotationEdit, fieldEdit } from "./edits";
import { refetchShowing, useWrite } from "./useWrite";
import { WriteStatus } from "./WriteStatus";

const ISSUE = editableFields("Issue");

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/**
 * A server holding one Issue: it serves the detail and the row, and applies
 * status (by compare-and-set) and title writes. `answer` can take a write over;
 * `change` edits the row as another user would.
 */
function serve(fields: { [field: string]: unknown } = {}) {
  let self: DetailRow = row("Issue", 1, { title: "Old", ...fields });
  const changes: Change[] = [];
  let held: Promise<void> | null = null;
  const server = {
    get self() {
      return self;
    },
    /** The row's edges, as its detail serves them. */
    edges: {} as PeersByEdge,
    /** Answers the next writes, in order, instead of the server. */
    answers: [] as (() => Response | Promise<Response>)[],
    sent: [] as Sent[],
    /** Hold the answer to the next read, as read now, until the returned function releases it. */
    holdNextRead() {
      let release = () => {};
      held = new Promise((resolve) => {
        release = resolve;
      });
      return () => release();
    },
    change(field: string, value: unknown, actor: string) {
      changes.unshift(change(self, changes.length + 1, { kind: field, actor, old: { [field]: self[field] }, new: { [field]: value } }));
      self = { ...self, [field]: value };
    },
    writes: () => server.sent.filter((request) => request.method !== "GET"),
  };
  server.sent = stubFetch(async (request) => {
    const path = new URL(request.url).pathname;
    if (request.method === "GET") {
      const answer = path === `/api/web/get/${self.id}` ? Response.json(detail(self, { changes, edges: server.edges })) : Response.json(self);
      const gate = held;
      held = null;
      if (gate) await gate;
      return answer;
    }
    const scripted = server.answers.shift();
    if (scripted) return scripted();
    const field = path.split("/").at(-1)!;
    const body = (await request.clone().json()) as { value?: unknown; expected?: unknown; mode?: string };
    if (body.mode === "cas" && body.expected !== self[field]) {
      return Response.json(
        { detail: `${field} transition rejected: expected '${body.expected}', found '${self[field]}'`, code: "conflict" },
        { status: 409 },
      );
    }
    if (self[field] === body.value) return Response.json({ id: self.id, change_id: null });
    server.change(field, body.value ?? null, "no-auth@localhost");
    return Response.json({ id: self.id, change_id: `c${changes.length}` });
  });
  return server;
}

/**
 * A control for one field of the served Issue, as an editor would build it on
 * the write layer; `onSettled` gets what each `run` resolved with.
 */
function Control({
  id,
  field,
  edit,
  onSettled = () => {},
}: {
  id: string;
  field: string;
  edit: (row: DetailRow) => Edit<unknown>;
  onSettled?: (result: unknown) => void;
}) {
  const query = useQuery(detailQueries.detail(id));
  const writer = useWrite();
  if (!query.data) return null;
  return (
    <div>
      <output aria-label={field}>{String(query.data.self[field])}</output>
      <button type="button" onClick={() => void writer.run(edit(query.data.self)).then(onSettled)} disabled={writer.state.status === "pending"}>
        Save
      </button>
      <WriteStatus state={writer.state} />
    </div>
  );
}

/**
 * Render `edit`'s control for the served Issue, once its detail has loaded.
 * `unmount` takes the control away and leaves the toasts; `settled` holds what
 * each of its `run`s resolved with.
 */
async function renderControl(
  server: ReturnType<typeof serve>,
  field: string,
  edit: (row: DetailRow) => Edit<unknown>,
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  const settled: unknown[] = [];
  const shown = (control: boolean) => (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        {control ? <Control id={server.self.id} field={field} edit={edit} onSettled={(result) => settled.push(result)} /> : null}
      </ToastProvider>
    </QueryClientProvider>
  );
  const view = render(shown(true));
  await screen.findByRole("button", { name: "Save" });
  return { settled, unmount: () => view.rerender(shown(false)) };
}

/** The field's shown value; a modal dialog hides the page from role queries, so hidden counts. */
const shown = (field: string) => screen.getByRole("status", { name: field, hidden: true }).textContent;

const setStatus = (to: string) => (self: DetailRow) =>
  fieldEdit({ id: self.id, field: "status", route: ISSUE.status!, label: "Status", from: self.status, to });

const setTitle = (to: string) => (self: DetailRow) =>
  fieldEdit({ id: self.id, field: "title", route: ISSUE.title!, label: "Title", from: self.title, to });

/** Press a toast's action from the keyboard: jsdom has no pointer capture for Radix's swipe. */
async function pressToast(name: string) {
  screen.getByRole("button", { name }).focus();
  await userEvent.keyboard("{Enter}");
}

test("a write shows pending, then refetches what it touched, then offers Undo", async () => {
  const server = serve();
  await renderControl(server, "status", setStatus("complete"));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(screen.getByRole("status", { name: "" }).textContent).toBe("Saving…");
  expect(screen.getByRole("button", { name: "Save" }).hasAttribute("disabled")).toBe(true);
  await screen.findByText("Status set to complete");
  expect(shown("status")).toBe("complete");
  expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
    `GET /api/web/get/${server.self.id}`,
    `PUT /api/inquiries/${server.self.id}/status`,
    `GET /api/web/get/${server.self.id}`,
  ]);
  expect(server.sent[1]!.body).toEqual({ value: "complete", mode: "cas", expected: "active" });
});

test("a 409 on compare-and-set opens the dialog with who changed what; saving mine expects theirs", async () => {
  const server = serve();
  await renderControl(server, "status", setStatus("complete"));
  server.change("status", "abandoned", "josh@example.com");
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  const dialog = await screen.findByRole("alertdialog", { name: "Status changed" });
  expect(within(dialog).getByText("josh@example.com changed this since you saw it. Save yours anyway?")).toBeTruthy();
  expect([...dialog.querySelectorAll("dt, dd")].map((part) => part.textContent)).toEqual([
    "Now, by josh@example.com",
    "abandoned",
    "Yours",
    "complete",
  ]);
  expect(shown("status")).toBe("abandoned");

  fireEvent.click(within(dialog).getByRole("button", { name: "Save mine" }));
  await screen.findByText("Status set to complete");
  expect(shown("status")).toBe("complete");
  const [first, again] = server.writes();
  expect(first!.body).toEqual({ value: "complete", mode: "cas", expected: "active" });
  expect(again!.body).toEqual({ value: "complete", mode: "cas", expected: "abandoned" });
  expect(again!.headers["idempotency-key"]).not.toBe(first!.headers["idempotency-key"]);
});

test("keeping theirs after a 409 sends nothing more, and Escape keeps theirs too", async () => {
  const server = serve();
  await renderControl(server, "status", setStatus("complete"));
  server.change("status", "invalid", "josh@example.com");
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  fireEvent.click(await screen.findByRole("button", { name: "Keep theirs" }));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  expect(shown("status")).toBe("invalid");

  server.change("status", "abandoned", "josh@example.com");
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByRole("alertdialog");
  await userEvent.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  expect(shown("status")).toBe("abandoned");
  expect(server.writes().map((request) => request.body)).toEqual([
    { value: "complete", mode: "cas", expected: "active" },
    { value: "complete", mode: "cas", expected: "invalid" },
  ]);
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a 409 on compare-and-set while the field has not moved shows the server's message, not the dialog", async () => {
  const server = serve();
  server.answers.push(() =>
    Response.json({ detail: "idempotency_key already used for a different operation", code: "conflict" }, { status: 409 }),
  );
  await renderControl(server, "status", setStatus("complete"));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("idempotency_key already used for a different operation"),
  );
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(server.writes()).toHaveLength(1);
});

test("a 5xx is retried with the same key and body; after three retries, Retry sends them once more", async () => {
  const server = serve();
  const unavailable = () => Response.json({ detail: "database unavailable" }, { status: 503 });
  server.answers.push(unavailable, unavailable, unavailable, unavailable);
  await renderControl(server, "status", setStatus("complete"));
  // After rendering: Testing Library's polling waits on the real `setTimeout`.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await act(() => vi.runAllTimersAsync());

  const alert = screen.getByRole("alert");
  expect(alert.textContent).toContain("Not saved: the server failed (database unavailable).");
  expect(server.writes()).toHaveLength(4);
  fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
  await act(() => vi.runAllTimersAsync());
  expect(shown("status")).toBe("complete");
  const writes = server.writes();
  expect(writes).toHaveLength(5);
  expect(new Set(writes.map((request) => JSON.stringify([request.headers, request.body]))).size).toBe(1);
});

test("Discard after a failure drops the edit", async () => {
  const server = serve();
  server.answers.push(...Array.from({ length: 4 }, () => () => new Response("bad gateway", { status: 502, statusText: "Bad Gateway" })));
  await renderControl(server, "status", setStatus("complete"));
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await act(() => vi.runAllTimersAsync());
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(shown("status")).toBe("active");
  expect(server.writes()).toHaveLength(4);
});

test("a failed write offers Copy details: the last request's id and attempt, and the retries before it", async () => {
  const copied = stubClipboard();
  const server = serve();
  server.answers.push(...Array.from({ length: 4 }, () => () => Response.json({ detail: "database unavailable" }, { status: 503 })));
  await renderControl(server, "status", setStatus("complete"));
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await act(() => vi.runAllTimersAsync());
  vi.useRealTimers();
  fireEvent.click(within(screen.getByRole("alert")).getByRole("button", { name: "Copy details" }));
  await waitFor(() => expect(copied).toHaveLength(1));
  const text = copied[0]!;
  expect(text).toMatch(/^Trackinizer web app: Not saved: the server failed \(database unavailable\)\.\n/);
  expect(text).toMatch(/\nfailed: request method=PUT path=\/api\/inquiries\/[0-9a-f-]+\/status status=503 ms=\d+ request_id=[0-9a-f-]{36} attempt=4 at=/);
  const key = [...text.matchAll(/ warn write\.failed route=setField key=(\S+) /g)].at(-1)![1];
  expect(text.split("\n").filter((line) => line.includes(`write.retry route=setField key=${key} `))).toHaveLength(3);
});

test("a refused write offers Copy details beside its message, outside the alert", async () => {
  const copied = stubClipboard();
  const server = serve();
  server.answers.push(() => Response.json({ detail: "title cannot be empty", code: "validation" }, { status: 400 }));
  await renderControl(server, "title", setTitle("New"));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect((await screen.findByRole("alert")).textContent).toBe("title cannot be empty");
  fireEvent.click(screen.getByRole("button", { name: "Copy details" }));
  await waitFor(() => expect(copied).toHaveLength(1));
  expect(copied[0]).toMatch(/\nfailed: request method=PUT path=\S+\/title status=400 code=validation /);
});

test("a field write whose answer was lost after someone else changed it opens the dialog with who, and is not resent", async () => {
  const server = serve();
  server.answers.push(() => {
    server.change("title", "Mine", "no-auth@localhost");
    server.change("title", "Theirs", "josh@example.com");
    return Promise.reject(new TypeError("Failed to fetch"));
  });
  await renderControl(server, "title", setTitle("Mine"));
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await act(() => vi.runAllTimersAsync());
  const dialog = screen.getByRole("alertdialog", { name: "Title changed", hidden: true });
  expect([...dialog.querySelectorAll("dt, dd")].map((part) => part.textContent)).toEqual([
    "Now, by josh@example.com",
    "Theirs",
    "Yours",
    "Mine",
  ]);
  fireEvent.click(within(dialog).getByRole("button", { name: "Keep theirs", hidden: true }));
  await act(() => vi.runAllTimersAsync());
  expect(shown("title")).toBe("Theirs");
  expect(server.writes()).toHaveLength(1);
});

// The read shows the value, not who set it: someone else may have saved the same
// value, and an Undo would then overwrite theirs.
test("a field write whose answer was lost after it landed says so without Undo, and is not resent", async () => {
  const server = serve();
  server.answers.push(() => {
    server.change("title", "Mine", "no-auth@localhost");
    return Promise.reject(new TypeError("Failed to fetch"));
  });
  await renderControl(server, "title", setTitle("Mine"));
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  // Only to the first retry: running every timer would also close the toast.
  await act(() => vi.advanceTimersByTimeAsync(1000));
  vi.useRealTimers();
  await screen.findByText("Title set to Mine");
  expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(server.writes()).toHaveLength(1);
});

test("an edge annotation whose answer was lost after someone else changed it names the change, and is not resent", async () => {
  const server = serve();
  const edge = { from: server.self.id, kind: "narrows", to: uuid(2) };
  server.answers.push(() => {
    server.edges = { narrows: [peer("Issue", 2, { note: "theirs" })] };
    return Promise.reject(new TypeError("Failed to fetch"));
  });
  await renderControl(server, "status", () => edgeAnnotationEdit({ edge, annotation: "note", label: "Note", from: "", to: "why" }));
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await act(() => vi.runAllTimersAsync());
  expect(screen.getByRole("alert").textContent).toBe("Not saved: someone changed Note to theirs");
  expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  expect(server.writes()).toHaveLength(1);
});

test("an edge write whose read finds it has not landed is sent again with the same key", async () => {
  const server = serve();
  server.answers.push(() => Response.json({ detail: "database unavailable" }, { status: 503 }));
  const edge = { from: server.self.id, kind: "narrows", to: "5d3c2b1a-0f9e-4d8c-8b7a-6e5d4c3b2a19" };
  await renderControl(server, "status", () => edgeAnnotationEdit({ edge, annotation: "note", label: "Note", from: "", to: "why" }));
  // After rendering: Testing Library's polling waits on the real `setTimeout`.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await act(() => vi.runAllTimersAsync());
  const writes = server.writes();
  expect(writes).toHaveLength(2);
  expect(writes[1]).toEqual(writes[0]);
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a 400 or a 422 shows the server's message beside the control, a 422 one line per field", async () => {
  const server = serve();
  server.answers.push(
    () =>
      Response.json(
        {
          detail: [
            { loc: ["body", "value"], msg: "String should have at least 1 character", type: "string_too_short" },
            { loc: ["body", "reason"], msg: "Input should be a valid string", type: "string_type" },
          ],
        },
        { status: 422 },
      ),
    () => Response.json({ detail: "title cannot be empty", code: "validation" }, { status: 400 }),
  );
  await renderControl(server, "title", setTitle("New"));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  const lines = (await screen.findByRole("alert")).querySelectorAll(".form-err > span");
  expect([...lines].map((line) => line.textContent)).toEqual([
    "value: String should have at least 1 character",
    "reason: Input should be a valid string",
  ]);
  expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("title cannot be empty"));
  expect(server.writes()).toHaveLength(2);
});

test("at save, a field another user changed shows both values and asks before writing", async () => {
  const server = serve();
  await renderControl(server, "title", setTitle("Mine"));
  server.change("title", "Theirs", "josh@example.com");
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  const dialog = await screen.findByRole("alertdialog", { name: "Title changed" });
  expect(within(dialog).getByText("It changed since you started editing. Save yours anyway?")).toBeTruthy();
  expect([...dialog.querySelectorAll("dd")].map((part) => part.textContent)).toEqual(["Theirs", "Mine"]);
  expect(server.writes()).toEqual([]);
  expect(server.sent.at(-1)).toMatchObject({ method: "GET", path: `/api/inquiries/${server.self.id}` });

  fireEvent.click(within(dialog).getByRole("button", { name: "Save mine" }));
  await screen.findByText("Title set to Mine");
  expect(server.writes().map((request) => request.body)).toEqual([{ value: "Mine" }]);
});

test("at save, an unchanged field writes straight away; keeping theirs writes nothing", async () => {
  const server = serve();
  await renderControl(server, "title", setTitle("Mine"));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Title set to Mine");
  expect(server.sent.slice(1, 3).map((request) => `${request.method} ${request.path}`)).toEqual([
    `GET /api/inquiries/${server.self.id}`,
    `PUT /api/inquiries/${server.self.id}/title`,
  ]);

  server.change("title", "Theirs", "josh@example.com");
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  fireEvent.click(await screen.findByRole("button", { name: "Keep theirs" }));
  await waitFor(() => expect(shown("title")).toBe("Theirs"));
  expect(server.writes()).toHaveLength(1);
});

test("Undo of a field edit is one new write back to the old value, checked at save like any edit", async () => {
  const server = serve();
  await renderControl(server, "title", setTitle("New"));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Title set to New");
  await pressToast("Undo");
  await screen.findByText("Title set to Old");
  expect(shown("title")).toBe("Old");
  const [edit, undo] = server.writes();
  expect(undo!.body).toEqual({ value: "Old" });
  expect(undo!.headers["idempotency-key"]).not.toBe(edit!.headers["idempotency-key"]);
  expect(server.sent.at(-3)).toMatchObject({ method: "GET", path: `/api/inquiries/${server.self.id}` });
  // The undo toast offers no undo of its own.
  expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
});

test("a write that changed nothing offers no Undo", async () => {
  const server = serve();
  await renderControl(server, "title", setTitle("Old"));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText("Title set to Old");
  expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
});

test("a 401 ends the session through the query cache; a 403 marks the profile for refetch", async () => {
  const server = serve();
  const onUnauthorized = vi.fn();
  server.answers.push(
    () => Response.json({ detail: "not signed in" }, { status: 401 }),
    () => Response.json({ detail: "writer role required" }, { status: 403 }),
  );
  const queryClient = createQueryClient(onUnauthorized);
  queryClient.setQueryData(bootQueries.profile.queryKey, { user_id: "u", email: "ada@example.com", name: "Ada", role: "writer", last_login: null, visual_workspace_enabled: false, ...AGREED });
  await renderControl(server, "title", setTitle("New"), queryClient);
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByRole("alert");
  expect(onUnauthorized).toHaveBeenCalledTimes(1);
  expect(queryClient.getQueryState(bootQueries.profile.queryKey)?.isInvalidated).toBe(false);

  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("Your role cannot make this change. writer role required"),
  );
  expect(queryClient.getQueryState(bootQueries.profile.queryKey)?.isInvalidated).toBe(true);
});

// A list learns of a row newly in it only from the live hub. With the stream
// down, a row the user just made would not show behind a form kept open by
// Create more; the write hands its rows to the hub as the stream would.
test("a landed write hands the live hub the rows it touched and made, as the stream's ids", async () => {
  const server = serve();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const hub = new LiveHub(queryClient);
  const change = vi.spyOn(hub, "change");
  const made = uuid(9);
  const edit = (self: DetailRow): Edit<unknown> => ({ ...setTitle("New")(self), creates: () => [made] });
  render(
    <QueryClientProvider client={queryClient}>
      <LiveContext value={hub}>
        <ToastProvider>
          <Control id={server.self.id} field="title" edit={edit} />
        </ToastProvider>
      </LiveContext>
    </QueryClientProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "Save" }));
  await screen.findByText("Title set to New");
  expect(change.mock.calls.map(([id]) => id)).toEqual([server.self.id, made]);
  hub.stop();
});

test("a landed write refetches every cached read that shows a touched row, and no other", async () => {
  const [a, b] = [row("Issue", 1), row("Issue", 2)];
  const queryClient = new QueryClient();
  const seed = (key: readonly unknown[], data: unknown) => queryClient.setQueryData(key, data);
  seed(["detail", a.id], detail(a));
  seed(["confidence", a.id], 0.5);
  seed(["detail", b.id], detail(b, { backlinks: { narrows: [{ id: a.id, kind: "Issue", seq: 1, title: "", status: "active" }] } }));
  seed(["inquiries", "list", [], "Issue", 50, 0], [a, b]);
  seed(["inquiries", "list", [{ field: "status", op: "ne", value: "active" }], "Issue", 50, 0], [b]);
  seed(["ref", "Issue", 1], a.id);
  seed(["meta", "enums"], { status: ["active"] });
  await refetchShowing(queryClient, [a.id]);
  const invalidated = queryClient
    .getQueryCache()
    .getAll()
    .filter((query) => query.state.isInvalidated)
    .map((query) => query.queryKey.slice(0, 2).join(" "));
  expect(invalidated).toEqual([`detail ${a.id}`, `confidence ${a.id}`, `detail ${b.id}`, "inquiries list"]);
});

test("after a 409, the conflict reads the row afresh, not through a read that began before the change (RV-02)", async () => {
  const server = serve();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await renderControl(server, "status", setStatus("complete"), queryClient);
  // A refetch starts while the row is still active, and its answer is slow.
  const release = server.holdNextRead();
  const reads = server.sent.length;
  void queryClient.refetchQueries({ queryKey: detailQueries.detail(server.self.id).queryKey });
  await waitFor(() => expect(server.sent).toHaveLength(reads + 1));
  server.change("status", "abandoned", "josh@example.com");
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(server.writes()).toHaveLength(1));
  release();
  const dialog = await screen.findByRole("alertdialog", { name: "Status changed" });
  expect([...dialog.querySelectorAll("dd")].map((part) => part.textContent)).toEqual(["abandoned", "complete"]);
});

test("a control that goes while it offers Retry settles its write, and a toast offers the Retry instead (R9-07)", async () => {
  const server = serve();
  const unavailable = () => Response.json({ detail: "database unavailable" }, { status: 503 });
  server.answers.push(unavailable, unavailable, unavailable, unavailable);
  const control = await renderControl(server, "status", setStatus("complete"));
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await act(() => vi.runAllTimersAsync());
  vi.useRealTimers();
  expect(screen.getByRole("button", { name: "Discard" })).toBeTruthy();

  control.unmount();
  await waitFor(() => expect(control.settled).toEqual([null]));
  expect(screen.getByText("Not saved: the server failed (database unavailable).")).toBeTruthy();
  await pressToast("Retry");
  await screen.findByText("Status set to complete");
  expect(server.self.status).toBe("complete");
  const writes = server.writes();
  expect(writes).toHaveLength(5);
  expect(new Set(writes.map((request) => request.headers["idempotency-key"])).size).toBe(1);
});

test("a control that goes while its conflict dialog is open keeps theirs, settles, and says so (R9-07)", async () => {
  const server = serve();
  const control = await renderControl(server, "status", setStatus("complete"));
  server.change("status", "abandoned", "josh@example.com");
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByRole("alertdialog", { name: "Status changed" });

  control.unmount();
  await waitFor(() => expect(control.settled).toEqual([null]));
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(screen.getByText("Not saved: josh@example.com changed Status to abandoned")).toBeTruthy();
  expect(server.writes()).toHaveLength(1);
});

test("a write whose control goes while it is sent still lands and toasts, and resolves null, so its flow stops (RV-04)", async () => {
  const server = serve();
  let answer = () => {};
  server.answers.push(async () => {
    await new Promise<void>((resolve) => {
      answer = resolve;
    });
    server.change("status", "complete", "no-auth@localhost");
    return Response.json({ id: server.self.id, change_id: "c1" });
  });
  const control = await renderControl(server, "status", setStatus("complete"));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(server.writes()).toHaveLength(1));

  control.unmount();
  answer();
  await screen.findByText("Status set to complete");
  expect(control.settled).toEqual([null]);
});
