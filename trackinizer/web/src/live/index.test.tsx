import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode, useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ActivityView } from "../activity";
import type { LoggedChange } from "../api/changes";
import type { InquiryRow } from "../api/inquiries";
import type { Profile } from "../api/me";
import { type Sent, stubFetch } from "../api/testing";
import { type Meta, MetaContext, ProfileContext } from "../app/boot";
import { createQueryClient } from "../app/queryClient";
import { CommandRegistry, CommandRegistryContext } from "../commands/registry";
import { DetailView } from "../detail";
import { detailQueries } from "../detail/queries";
import { ListView } from "../lists";
import { RouterProvider } from "../router/router";
import { PausedBar } from "../ui/bars";
import { ToastProvider } from "../ui/toast";
import { LiveProvider, useLiveDetail } from ".";
import { answerRows, change, FakeEventSource, issue, listParams, serveChanges, serveRows, uuid } from "./testing";

const KINDS = ["Issue"];
const META: Meta = {
  enums: { inquiry_kind_all: KINDS, status: ["active", "complete", "abandoned", "invalid"] },
  fieldOwners: { priority: "issue" },
  edges: {},
  kinds: KINDS,
};
const PROFILE: Profile = { user_id: "u1", email: "ada@example.com", name: "Ada", role: "writer", last_login: null, visual_workspace_enabled: false };

let server: InquiryRow[];
let sent: Sent[];

beforeEach(() => {
  // The clock moves with real time, in steps small enough that `waitFor` polls
  // every few milliseconds instead of every 20 (the default step).
  vi.useFakeTimers({ shouldAdvanceTime: true, advanceTimeDelta: 2 });
  sessionStorage.clear();
  FakeEventSource.reset();
  vi.stubGlobal("EventSource", FakeEventSource);
  Element.prototype.scrollIntoView = () => {};
  server = [];
  sent = serveRows(
    () => server,
    (request) => {
      const row = server.find((r) => new URL(request.url).pathname === `/api/web/get/${r.id}`);
      if (!row) return Response.json({ detail: "not found" }, { status: 404 });
      return Response.json({ self: { ...row, description: null }, edges: {}, backlinks: {}, changes: [] });
    },
  );
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Render `ui` with the app's providers and the live stream, on its fake; returns the query cache. */
function show(ui: ReactNode, client = createQueryClient(() => {})) {
  render(
    <QueryClientProvider client={client}>
      <CommandRegistryContext value={new CommandRegistry()}>
        <ToastProvider>
          <MetaContext value={META}>
            <ProfileContext value={PROFILE}>
              <RouterProvider kinds={KINDS}>
                <LiveProvider>{ui}</LiveProvider>
              </RouterProvider>
            </ProfileContext>
          </MetaContext>
        </ToastProvider>
      </CommandRegistryContext>
    </QueryClientProvider>,
  );
  return client;
}

/**
 * An editor as the plan has them: its draft in its own state, keyed by row and
 * field, never in the cached row, which live updates replace.
 */
function DraftEditor({ id }: { id: string }) {
  const detail = useQuery(detailQueries.detail(id));
  useLiveDetail(id);
  const [draft, setDraft] = useState("");
  return (
    <section aria-label="Editor">
      <p data-testid="editing">{detail.data?.self.title}</p>
      <textarea aria-label="Draft" value={draft} onChange={(event) => setDraft(event.target.value)} />
    </section>
  );
}

/** `waitFor`, checking every 2 ms rather than every 50. */
const until = <T,>(check: () => T) => waitFor(check, { interval: 2 });

const rowElement = (seq: number) => document.querySelector<HTMLElement>(`[data-row="${uuid(seq)}"]`);
const rowTitle = (seq: number) => rowElement(seq)?.querySelector(".row-title")?.textContent;
const scroller = () => document.querySelector<HTMLElement>(".scroll")!;

/** The stream delivers `seqs`' ids, and the one-second window closes. */
async function stream(...seqs: number[]) {
  for (const seq of seqs) FakeEventSource.last.send(uuid(seq));
  await act(() => vi.advanceTimersByTimeAsync(1_000));
}

test("a batch updates a row in place; an open draft, the focus and the scroll position survive it", async () => {
  server = [1, 2, 3, 4, 5].map((seq) => issue(seq));
  show(
    <>
      <ListView kind="Issue" />
      <DetailView target={{ id: uuid(5) }} />
      <DraftEditor id={uuid(5)} />
    </>,
  );
  await until(() => expect(rowTitle(5)).toBe("Issue 5"));
  await until(() => expect(screen.getByTestId("editing").textContent).toBe("Issue 5"));
  // The stream replays nothing from before it opened, and the list and detail
  // read before it did: its first open catches both up, once.
  const beforeOpen = sent.length;
  FakeEventSource.last.open();
  await until(() => expect(sent.length).toBe(beforeOpen + 2));
  expect(sent.slice(beforeOpen).map((s) => s.path).toSorted()).toEqual(["/api/inquiries", `/api/web/get/${uuid(5)}`]);
  const row = rowElement(5);
  scroller().scrollTop = 120;
  const draft = screen.getByLabelText<HTMLTextAreaElement>("Draft");
  draft.focus();
  fireEvent.change(draft, { target: { value: "Half-written reply" } });
  const before = sent.length;

  server[4] = issue(5, { title: "Renamed by someone else" });
  await stream(5);

  await until(() => expect(rowTitle(5)).toBe("Renamed by someone else"));
  await until(() => expect(document.querySelector(".d-title")?.textContent).toBe("Renamed by someone else"));
  await until(() => expect(screen.getByTestId("editing").textContent).toBe("Renamed by someone else"));
  expect(rowElement(5)).toBe(row);
  expect(draft.value).toBe("Half-written reply");
  expect(document.activeElement).toBe(draft);
  expect(scroller().scrollTop).toBe(120);
  // One list request for just that row, and one detail read for the detail and
  // the editor, which share it.
  const requests = sent.slice(before);
  expect(requests.filter((s) => s.path === "/api/inquiries").map((s) => listParams(s).seqRanges)).toEqual([["5..5"]]);
  expect(requests.filter((s) => s.path.startsWith("/api/web/get/"))).toHaveLength(1);
});

test("a live change never moves a row to another group; the fresh grouping waits for the user", async () => {
  server = [issue(1, { priority: 20 }), issue(2, { priority: 20 }), issue(3, { priority: 30 })];
  show(<ListView kind="Issue" />);
  await until(() => expect(rowTitle(1)).toBe("Issue 1"));
  const group = (seq: number) => rowElement(seq)?.closest("section")?.getAttribute("aria-label");
  expect(group(1)).toBe("P2 Medium");
  server[0] = issue(1, { priority: 0, title: "Now urgent" });
  await stream(1);
  await until(() => expect(rowTitle(1)).toBe("Now urgent"));
  expect(group(1)).toBe("P2 Medium");
});

test("a tab the user picks lays the rows out afresh, even when it holds the same rows", async () => {
  server = [issue(1, { priority: 20 }), issue(2, { priority: 20 })];
  show(<ListView kind="Issue" />);
  await until(() => expect(rowTitle(1)).toBe("Issue 1"));
  const group = (seq: number) => rowElement(seq)?.closest("section")?.getAttribute("aria-label");
  // Visit All once, so its pages are cached and show at once when picked again.
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  await until(() => expect(sent.filter((s) => s.path === "/api/inquiries")).toHaveLength(2));
  fireEvent.click(screen.getByRole("button", { name: "Active" }));
  await until(() => expect(rowTitle(1)).toBe("Issue 1"));
  server[0] = issue(1, { priority: 0, title: "Now urgent" });
  await stream(1);
  await until(() => expect(rowTitle(1)).toBe("Now urgent"));
  expect(group(1)).toBe("P2 Medium");
  // Every row is active, so All holds the same rows as Active.
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  await until(() => expect(group(1)).toBe("P0 Critical"));
});

test("a new matching row waits behind the pill while the list is scrolled; the pill brings it to the top", async () => {
  server = [1, 2, 3].map((seq) => issue(seq));
  show(<ListView kind="Issue" />);
  await until(() => expect(rowTitle(3)).toBe("Issue 3"));
  scroller().scrollTop = 200;
  server.push(issue(4, { title: "Brand new" }));
  await stream(4);
  const pill = await until(() => screen.getByRole("button", { name: "1 new" }));
  expect(rowElement(4)).toBeNull();
  fireEvent.click(pill);
  await until(() => expect(rowTitle(4)).toBe("Brand new"));
  expect(scroller().scrollTop).toBe(0);
  expect(screen.queryByRole("button", { name: /new$/ })).toBeNull();
});

test("the pill brings the user to the new row when it joins further down, as in a lower priority group", async () => {
  server = [1, 2, 3].map((seq) => issue(seq, { priority: 20 }));
  show(<ListView kind="Issue" />);
  await until(() => expect(rowTitle(3)).toBe("Issue 3"));
  scroller().scrollTop = 200;
  server.push(issue(4, { title: "No priority yet" }));
  await stream(4);
  const pill = await until(() => screen.getByRole("button", { name: "1 new" }));
  const scrolledTo = vi.spyOn(Element.prototype, "scrollIntoView");
  fireEvent.click(pill);
  expect(rowTitle(4)).toBe("No priority yet");
  expect(rowElement(4)?.closest("section")?.getAttribute("aria-label")).toBe("No priority");
  expect(scrolledTo.mock.contexts).toEqual([rowElement(4)]);
});

test("at the top of the list, a new row joins once the user has been idle for 2 s", async () => {
  server = [issue(1)];
  show(<ListView kind="Issue" />);
  await until(() => expect(rowTitle(1)).toBe("Issue 1"));
  fireEvent.keyDown(document.body, { key: "j" });
  server.push(issue(2));
  await stream(2);
  await until(() => screen.getByRole("button", { name: "1 new" }));
  await act(() => vi.advanceTimersByTimeAsync(1_500));
  await until(() => expect(rowTitle(2)).toBe("Issue 2"));
  expect(screen.queryByRole("button", { name: /new$/ })).toBeNull();
});

test("a row that stops matching stays, dimmed", async () => {
  server = [issue(1), issue(2)];
  show(<ListView kind="Issue" />);
  await until(() => expect(rowTitle(2)).toBe("Issue 2"));
  server[1] = issue(2, { status: "complete" });
  await stream(2);
  await until(() => expect(document.querySelector(".scroll style")?.textContent).toContain(uuid(2)));
  expect(rowTitle(2)).toBe("Issue 2");
});

test("the paused bar shows once the stream has been down 10 s; the reconnect hides it and reloads the first page", async () => {
  server = [issue(1)];
  show(
    <>
      <PausedBar />
      <ListView kind="Issue" />
    </>,
  );
  await until(() => expect(rowTitle(1)).toBe("Issue 1"));
  FakeEventSource.last.open();
  // The 10 s edge holds only on a clock that moves when told. This file's clock
  // also moves with real time, so a busy run passed 10 s inside the 9,999 ms
  // advance (measured: 10,003 ms) and showed the bar early.
  vi.setTimerTickMode("manual");
  FakeEventSource.last.drop();
  await act(() => vi.advanceTimersByTimeAsync(9_999));
  expect(screen.queryByRole("status")).toBeNull();
  await act(() => vi.advanceTimersByTimeAsync(1));
  expect(screen.getByRole("status").textContent).toBe("Live updates paused. Reconnecting…");
  vi.setTimerTickMode("interval", 2);
  const before = sent.length;
  server[0] = issue(1, { title: "Changed while down" });
  act(() => FakeEventSource.last.open());
  expect(screen.queryByRole("status")).toBeNull();
  await until(() => expect(rowTitle(1)).toBe("Changed while down"));
  expect(sent.slice(before).map((s) => listParams(s))[0]).toMatchObject({ seqRanges: [], limit: 1_000, offset: 0 });
});

test("a change from the stream joins the top of Activity, and lines already shown keep their lookups", async () => {
  server = Array.from({ length: 30 }, (_, n) => issue(n + 1));
  const changes: LoggedChange[] = Array.from({ length: 20 }, (_, n) => change(n + 1));
  sent = serveChanges(
    () => changes,
    (request) => answerRows(server, request) ?? Response.json({ detail: "not found" }, { status: 404 }),
  );
  show(<ActivityView />);
  await until(() => expect(document.querySelectorAll(".feed-row")).toHaveLength(20));
  await until(() => expect(document.querySelector(".feed-row .feed-title")?.textContent).toBe(" Issue 20"));
  FakeEventSource.last.open();
  const lookups = () => sent.filter((s) => s.path === "/api/inquiries").length;
  const before = lookups();
  // A change to a row the feed already names, then one about a new row.
  changes.push(change(21, { kind: "status", subject_id: uuid(3), old: { status: "active" }, new: { status: "complete" } }));
  await stream(3);
  await until(() => expect(document.querySelectorAll(".feed-row")).toHaveLength(21));
  expect(document.querySelector(".feed-row .tl-body")?.textContent).toContain("changed status from active to complete");
  expect(lookups()).toBe(before);
  await act(() => vi.advanceTimersByTimeAsync(2_000));
  changes.push(change(22));
  await stream(22);
  await until(() => expect(document.querySelector(".feed-row .feed-title")?.textContent).toBe(" Issue 22"));
  expect(lookups()).toBe(before + 1);
});

test("a stream that never opens counts as down: the paused bar shows after 10 s (CR-LIVE-R4-A1)", async () => {
  show(<PausedBar />);
  vi.setTimerTickMode("manual");
  await act(() => vi.advanceTimersByTimeAsync(9_999));
  expect(screen.queryByRole("status")).toBeNull();
  await act(() => vi.advanceTimersByTimeAsync(1));
  expect(screen.getByRole("status").textContent).toBe("Live updates paused. Reconnecting…");
  vi.setTimerTickMode("interval", 2);
});

/** Rest the pointer where the elements `under` picks are, as `:hover` reports it; jsdom has no pointer. */
function hover(under: (element: Element) => boolean) {
  const matches = Element.prototype.matches;
  vi.spyOn(Element.prototype, "matches").mockImplementation(function (this: Element, selector: string) {
    return selector === ":hover" ? under(this) : matches.call(this, selector);
  });
}

test("at the top of an idle list, a new row waits behind the pill while the pointer rests on its rows (CR-LIVE-R4-A3)", async () => {
  hover((element) => element.classList.contains("scroll") || element.tagName === "SECTION");
  server = [issue(1)];
  show(<ListView kind="Issue" />);
  await until(() => expect(rowTitle(1)).toBe("Issue 1"));
  server.push(issue(2));
  await stream(2);
  await until(() => screen.getByRole("button", { name: "1 new" }));
  await act(() => vi.advanceTimersByTimeAsync(3_000));
  expect(rowElement(2)).toBeNull();
  expect(screen.getByRole("button", { name: "1 new" })).toBeTruthy();
});

test("a pointer resting on the list but on no row holds nothing: at the top of an idle list, a new row joins (CR-LIVE-R4-A3)", async () => {
  // As after picking a filter from a menu over an empty list.
  hover((element) => element.classList.contains("scroll"));
  show(<ListView kind="Issue" />);
  await until(() => screen.getByText("Nothing here"));
  server.push(issue(1));
  await stream(1);
  await act(() => vi.advanceTimersByTimeAsync(2_000));
  await until(() => expect(rowTitle(1)).toBe("Issue 1"));
  expect(screen.queryByRole("button", { name: /new$/ })).toBeNull();
});

test("the pill opens a collapsed group a new row joins, so the row shows (CR-LIVE-R7-B2)", async () => {
  server = [1, 2, 3].map((seq) => issue(seq, { priority: 20 }));
  show(<ListView kind="Issue" />);
  await until(() => expect(rowTitle(3)).toBe("Issue 3"));
  server.push(issue(4, { title: "Joins a collapsed group", priority: 20 }));
  fireEvent.click(screen.getByRole("button", { name: /P2 Medium/ }));
  expect(rowElement(3)).toBeNull();
  scroller().scrollTop = 200;
  await stream(4);
  fireEvent.click(await until(() => screen.getByRole("button", { name: "1 new" })));
  await until(() => expect(rowTitle(4)).toBe("Joins a collapsed group"));
});

test("a new row that a page read brings in is no longer counted in the pill (CR-LIVE-R9-A1)", async () => {
  server = [issue(1)];
  const client = show(<ListView kind="Issue" />);
  await until(() => expect(rowTitle(1)).toBe("Issue 1"));
  scroller().scrollTop = 200;
  server.push(issue(2));
  await stream(2);
  await until(() => screen.getByRole("button", { name: "1 new" }));
  // A read of the list's pages, as after the user's own write, brings the new row in.
  await act(() => client.refetchQueries({ queryKey: ["inquiries", "list"] }));
  await until(() => expect(rowTitle(2)).toBe("Issue 2"));
  expect(screen.queryByRole("button", { name: /new$/ })).toBeNull();
});

test("live updates that keep failing say so with Retry, after the first try and two retries (CR-R3-B4)", async () => {
  server = [issue(1)];
  let failing = true;
  stubFetch((request) =>
    failing && new URL(request.url).searchParams.has("seq_range")
      ? Response.json({ detail: "database unavailable" }, { status: 503 })
      : (answerRows(server, request) ?? Response.json({ detail: "not found" }, { status: 404 })),
  );
  show(<ListView kind="Issue" />);
  await until(() => expect(rowTitle(1)).toBe("Issue 1"));
  server[0] = issue(1, { title: "Renamed" });
  await stream(1);
  await act(() => vi.advanceTimersByTimeAsync(1_000));
  expect(screen.queryByRole("status")).toBeNull();
  await act(() => vi.advanceTimersByTimeAsync(3_000));
  await until(() => expect(screen.getByRole("status").textContent).toBe("Live updates failed: database unavailableRetry"));
  failing = false;
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await until(() => expect(rowTitle(1)).toBe("Renamed"));
  expect(screen.queryByRole("status")).toBeNull();
});

test("Activity's live updates that keep failing say so with Retry (CR-R3-B4)", async () => {
  let failing = true;
  sent = serveChanges(
    () => [change(1)],
    (request) => answerRows([issue(1)], request) ?? Response.json({ detail: "not found" }, { status: 404 }),
  );
  const answer = globalThis.fetch;
  vi.stubGlobal("fetch", (request: Request) =>
    failing && new URL(request.url).searchParams.has("since")
      ? Promise.resolve(Response.json({ detail: "database unavailable" }, { status: 503 }))
      : answer(request),
  );
  show(<ActivityView />);
  await until(() => expect(document.querySelectorAll(".feed-row")).toHaveLength(1));
  await stream(1);
  // It asks at 1, 3 and 6 s: the first retry, due at 2 s, waits out the 2 s between asks.
  await act(() => vi.advanceTimersByTimeAsync(5_000));
  await until(() => expect(screen.getByRole("status").textContent).toBe("Live updates failed: database unavailableRetry"));
  failing = false;
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  // The retry asks as soon as the 2 s between asks allow.
  await act(() => vi.advanceTimersByTimeAsync(2_000));
  await until(() => expect(screen.queryByRole("status")).toBeNull());
});
