import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { LoggedChange } from "../api/changes";
import { type Sent, stubFetch } from "../api/testing";
import { MetaContext } from "../app/boot";
import { META, uuid } from "../detail/testing";
import { RouterProvider } from "../router/router";
import { refetchShowing } from "../writes/useWrite";
import { ActivityView } from ".";
import { tabKinds } from "./feed";
import { activityQueries } from "./queries";

/** Change number `n`: `n` minutes before 09:00 local time on 2026-09-26, a Saturday. */
function change(n: number, fields: Partial<LoggedChange> = {}): LoggedChange {
  return {
    id: uuid(n),
    created: new Date(NOW - n * 60_000).toISOString(),
    actor: "ada@example.com",
    kind: "created",
    subject_id: uuid(100 + n),
    subject_kind: "Issue",
    caused_by: null,
    reason: "",
    old: {},
    new: {},
    ...fields,
  };
}

const NOW = new Date(2026, 8, 26, 9, 0).getTime();

/**
 * Answer `GET /api/change_log` from `changes` as the server does: the kinds the
 * repeated `kind` names, newest first, after `after_id`, up to `limit`. Answer the subject lookup with
 * every inquiry the `id` filter names, as `Issue#<n>` titled `Inquiry <n>`, save
 * those in `purged`. A request `fail` answers for gets its answer instead.
 */
function serve(
  changes: readonly LoggedChange[],
  { purged = [], fail = () => null }: { purged?: readonly string[]; fail?: (query: URLSearchParams) => Response | null } = {},
): Sent[] {
  return stubFetch((request) => {
    const url = new URL(request.url);
    const query = url.searchParams;
    const failure = fail(query);
    if (failure) return failure;
    if (url.pathname === "/api/change_log") {
      const ofKind = changes
        .filter((c) => query.getAll("kind").includes(c.kind))
        .toSorted((a, b) => Date.parse(b.created) - Date.parse(a.created) || (a.id < b.id ? 1 : -1));
      const after = ofKind.findIndex((c) => c.id === query.get("after_id"));
      return Response.json(ofKind.slice(after + 1, after + 1 + Number(query.get("limit"))));
    }
    const pattern = JSON.parse(query.get("filter")!).value as string;
    const ids = pattern.slice(2, -2).split("|");
    return Response.json(
      ids
        .filter((id) => !purged.includes(id))
        .map((id) => ({ id, kind: "Issue", seq: Number(id.slice(-6)), title: `Inquiry ${Number(id.slice(-6))}` })),
    );
  });
}

function show(queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  render(
    <QueryClientProvider client={queryClient}>
      <MetaContext value={META}>
        <RouterProvider kinds={META.kinds}>
          <ActivityView />
        </RouterProvider>
      </MetaContext>
    </QueryClientProvider>,
  );
}

/** The lines on screen, as text. */
function lines(): string[] {
  return [...document.querySelectorAll(".feed-row .tl-body")].map((line) => line.textContent!);
}

/** Wait for the lines to read `expected`, their subjects looked up. */
async function expectLines(expected: readonly string[]) {
  await waitFor(() => expect(lines()).toEqual(expected));
}

function changeLogRequests(sent: readonly Sent[]): string[] {
  return sent
    .filter((request) => request.path === "/api/change_log")
    .map((request) => {
      const query = new URLSearchParams(request.query);
      return [query.getAll("kind").join(","), query.get("after_id") ?? "-"].join(" ");
    });
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  history.replaceState(null, "", "#/activity");
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("All reads every tab's kinds in one request a page, newest first, by day (PA2)", async () => {
  const sent = serve([
    change(5, { kind: "status", old: { status: "active" }, new: { status: "complete" }, reason: "Fixed in Issue#7." }),
    change(10),
    change(24 * 60, { kind: "description", subject_id: uuid(440), old: { description: "a" }, new: { description: "b" } }),
    change(3, { kind: "dependency_changed" }),
  ]);
  show();
  await expectLines([
    "ada@example.com changed status from active to complete on Issue#105 Inquiry 105Fixed in Issue#7.",
    "ada@example.com created the issue on Issue#110 Inquiry 110",
    "ada@example.com edited the description on Issue#440 Inquiry 440",
  ]);
  expect(changeLogRequests(sent)).toEqual([
    "status,belief_judgement,created,purged,edge_added,edge_removed,description,title,issue_priority,labels,owner -",
  ]);
  // Brief rows: the feed reads set-or-not and ids, never a description's text.
  const briefs = sent.filter((request) => request.path === "/api/change_log").map((r) => new URLSearchParams(r.query).get("brief"));
  expect(new Set(briefs)).toEqual(new Set(["true"]));
  expect([...document.querySelectorAll(".feed-day")].map((day) => day.textContent)).toEqual(["Today", "Yesterday"]);
  expect(within(screen.getByRole("navigation", { name: "Change kind" })).getByRole("button", { name: "All" }).getAttribute("aria-current")).toBe("page");
});

test("each line links to its inquiry, first by id, then by Kind#seq once looked up", async () => {
  const sent = serve([change(1), change(2)], { purged: [uuid(102)] });
  show();
  await waitFor(() => expect(screen.getByRole("link", { name: "Issue#101" })).toBeTruthy());
  expect(screen.getByRole("link", { name: "Issue#101" }).getAttribute("href")).toBe("#/ref/Issue/101");
  expect(screen.getByText("Inquiry 101")).toBeTruthy();
  // Purged: the lookup does not find it, so it stays a link by id, which says it is gone.
  expect(screen.getByRole("link", { name: "Issue 00000000" }).getAttribute("href")).toBe(`#/lookup/${uuid(102)}`);
  const lookups = sent.filter((request) => request.path === "/api/inquiries");
  expect(lookups).toHaveLength(1);
  // Oldest mention first, so live lines joining at the top leave older lookups be.
  expect(new URLSearchParams(lookups[0]!.query).get("filter")).toBe(
    JSON.stringify({ field: "id", op: "re", value: `^(${uuid(102)}|${uuid(101)})$` }),
  );
});

test("an edge change reads as one line on the child, naming the parent", async () => {
  const [child, parent] = [uuid(101), uuid(102)];
  const first = change(1, { kind: "edge_added", subject_id: child, new: { peer_id: parent, peer_kind: "Issue", peer_edge_kind: "requires" } });
  const second = change(1, {
    id: uuid(2),
    kind: "edge_added",
    subject_id: parent,
    caused_by: first.id,
    new: { peer_id: child, peer_kind: "Issue", peer_edge_kind: "requires" },
  });
  serve([second, first]);
  show();
  fireEvent.click(screen.getByRole("button", { name: "Relations" }));
  await expectLines(["ada@example.com added relation Requires Issue#102 on Issue#101 Inquiry 101"]);
});

test("a tab reads its kinds in one request, and Load more asks for the changes after the last shown", { tags: ["manual"] }, async () => {
  const statuses = Array.from({ length: 60 }, (_, k) =>
    change(k + 1, { kind: "status", old: { status: "active" }, new: { status: "complete" } }),
  );
  const sent = serve(statuses);
  show();
  fireEvent.click(screen.getByRole("button", { name: "Status" }));
  await waitFor(() => expect(lines()).toHaveLength(50));
  expect(screen.getByText("50 shown")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  await waitFor(() => expect(lines()).toHaveLength(60));
  expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  expect(changeLogRequests(sent).filter((request) => request.startsWith("status "))).toEqual([
    "status -",
    `status ${uuid(50)}`,
  ]);
});

test("the tab and its pages last for this browser tab, so Back finds the feed as it was", async () => {
  serve([change(1), change(2, { kind: "status", old: { status: "active" }, new: { status: "abandoned" } })]);
  show();
  fireEvent.click(screen.getByRole("button", { name: "Status" }));
  await waitFor(() => expect(lines()).toHaveLength(1));
  cleanup();
  show();
  expect(screen.getByRole("button", { name: "Status" }).getAttribute("aria-current")).toBe("page");
  await expectLines(["ada@example.com changed status from active to abandoned on Issue#102 Inquiry 102"]);
});

test("a read with nothing loaded shows the server's message in the feed's place, with Retry", async () => {
  let refuse = true;
  serve([change(1)], { fail: () => (refuse ? Response.json({ detail: "statement timeout" }, { status: 500 }) : null) });
  show();
  expect((await screen.findByRole("alert")).textContent).toBe("statement timeoutRetry");
  refuse = false;
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await expectLines(["ada@example.com created the issue on Issue#101 Inquiry 101"]);
});

test("a stored state this build cannot read, such as a page count that is not one, starts afresh (STATE-02)", async () => {
  sessionStorage.setItem("trackinizer.v2.activity", JSON.stringify({ tab: "status", pages: { status: "many" } }));
  serve([change(2, { kind: "status", old: { status: "active" }, new: { status: "abandoned" } })]);
  show();
  await expectLines(["ada@example.com changed status from active to abandoned on Issue#102 Inquiry 102"]);
});

test("a failed subject lookup says so with Retry, and the lines fill in after it (READ-02)", async () => {
  let refuse = true;
  serve([change(1)], {
    fail: (query) => (refuse && query.has("filter") ? Response.json({ detail: "statement timeout" }, { status: 500 }) : null),
  });
  show();
  expect((await screen.findByRole("status")).textContent).toBe("Could not look up the inquiries named: statement timeoutRetry");
  expect(lines()).toEqual(["ada@example.com created the issue on Issue 00000000"]);
  refuse = false;
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await expectLines(["ada@example.com created the issue on Issue#101 Inquiry 101"]);
});

test("a subject's title is read afresh: on return to Activity, and after a write that touched it (READ-04)", async () => {
  let title = "Before";
  const sent = stubFetch((request) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/change_log") {
      return Response.json(url.searchParams.getAll("kind").includes("created") ? [change(1)] : []);
    }
    return Response.json([{ id: uuid(101), kind: "Issue", seq: 101, title }]);
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  show(queryClient);
  await waitFor(() => expect(screen.getByText("Before")).toBeTruthy());
  title = "After a write";
  await refetchShowing(queryClient, [uuid(101)]);
  await waitFor(() => expect(screen.getByText("After a write")).toBeTruthy());
  cleanup();
  title = "After another client";
  show(queryClient);
  await waitFor(() => expect(screen.getByText("After another client")).toBeTruthy());
  expect(sent.filter((request) => request.path === "/api/inquiries")).toHaveLength(3);
});

test("title, priority, label and owner edits show under Edits, a relation removed under Relations, a purge under Created (PA2)", async () => {
  const [child, parent] = [uuid(101), uuid(102)];
  serve([
    change(1, { kind: "title", old: { title: "Old" }, new: { title: "New" } }),
    change(2, { kind: "issue_priority", old: { issue_priority: 20 }, new: { issue_priority: 10 } }),
    change(3, { kind: "labels", new: { labels: ["ui"] } }),
    change(4, { kind: "owner", new: { owner: "bo@example.com" } }),
    change(5, { kind: "edge_removed", subject_id: child, old: { peer_id: parent, peer_kind: "Issue", peer_edge_kind: "requires" } }),
    change(6, { kind: "purged", reason: "A duplicate." }),
  ]);
  show();
  await waitFor(() => expect(lines()).toHaveLength(6));
  fireEvent.click(screen.getByRole("button", { name: "Edits" }));
  await expectLines([
    "ada@example.com changed the title on Issue#101 Inquiry 101",
    "ada@example.com changed priority from 20 to 10 on Issue#102 Inquiry 102",
    "ada@example.com added label ui on Issue#103 Inquiry 103",
    "ada@example.com set the owner to bo@example.com on Issue#104 Inquiry 104",
  ]);
  fireEvent.click(screen.getByRole("button", { name: "Relations" }));
  await expectLines(["ada@example.com removed relation Requires Issue#102 on Issue#101 Inquiry 101"]);
  fireEvent.click(screen.getByRole("button", { name: "Created" }));
  await expectLines(["ada@example.com purged the issue on Issue#106 Inquiry 106A duplicate."]);
});

test("a change both the live head and a refetched first page hold shows once", async () => {
  serve([change(1)]);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(activityQueries.head(tabKinds("all")).queryKey, [change(1)]);
  queryClient.setQueryData(activityQueries.page(tabKinds("all"), null).queryKey, [change(1)]);
  show(queryClient);
  await screen.findByText("Inquiry 101");
  expect(lines()).toEqual(["ada@example.com created the issue on Issue#101 Inquiry 101"]);
});

test("a tab with no changes says so", async () => {
  serve([change(1)]);
  show();
  await waitFor(() => expect(lines()).toHaveLength(1));
  fireEvent.click(screen.getByRole("button", { name: "Judgements" }));
  expect((await screen.findByRole("heading", { name: "No activity" })).nextElementSibling!.textContent).toBe(
    "No changes of this kind yet.",
  );
});

test("after a later page fails, Load more asks for that page again (CR-LIVE-R5-B2)", { tags: ["manual"] }, async () => {
  sessionStorage.setItem("trackinizer.v2.activity", JSON.stringify({ tab: "status", pages: 1 }));
  const statuses = Array.from({ length: 51 }, (_, k) =>
    change(k + 1, { kind: "status", old: { status: "active" }, new: { status: "complete" } }),
  );
  let refuse = true;
  const sent = serve(statuses, {
    fail: (query) => (refuse && query.get("after_id") ? Response.json({ detail: "statement timeout" }, { status: 500 }) : null),
  });
  show();
  await waitFor(() => expect(lines()).toHaveLength(50));
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  await screen.findByRole("status");
  refuse = false;
  fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  await waitFor(() => expect(lines()).toHaveLength(51));
  expect(changeLogRequests(sent).filter((request) => request === `status ${uuid(50)}`)).toHaveLength(2);
});

test("a long title is clipped between characters, never inside one (CR-LIVE-R5-A1)", async () => {
  const title = `${"a".repeat(68)}😀 and more`;
  stubFetch((request) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/change_log") return Response.json(url.searchParams.getAll("kind").includes("created") ? [change(1)] : []);
    return Response.json([{ id: uuid(101), kind: "Issue", seq: 101, title }]);
  });
  show();
  await waitFor(() => expect(document.querySelector(".feed-title")?.textContent).toBe(` ${"a".repeat(68)}😀…`));
});
